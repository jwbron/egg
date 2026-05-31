# BRC Consensus History — refine phase

Generated: 2026-05-31T20:04:00Z
Pipeline: issue-2908

### [2026-05-31T19:45:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: f8956842-8ac2-49
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:42.472740+00:00'
````

### [2026-05-31T19:46:23Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 07f7d19f-47e0-4f
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:57.622574+00:00'
````

### [2026-05-31T19:46:23Z] overseer (HEARTBEAT): heartbeat: WORKING

Cycle 1 complete. Pipeline issue-2908 is running (refine phase). Three agents active: refiner, reviewer_refine, reviewer_agent_design (all ~58s elapsed). Orchestrator status endpoint unreachable; BRC/contract endpoints healthy. No tier-1 alerts; advisor gate closed. Monitoring continues.

````yaml
id: 45f1e12a-a9fb-41
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T19:46:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 95fcab7c-162a-45
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:42.472740+00:00'
````

### [2026-05-31T19:47:24Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 80ceb8c5-686a-43
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:57.622574+00:00'
````

### [2026-05-31T19:47:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: fbbc88cb-b253-49
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:42.472740+00:00'
````

### [2026-05-31T19:48:24Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 1d65d5ea-0b3a-45
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:57.622574+00:00'
````

### [2026-05-31T19:48:48Z] overseer (OVERSEER_ALERT): agent-heartbeat-stall [high]

Refiner agent stalled in refine phase — no BRC signals after 192s (threshold: 180s)

Detail:
The `refiner` agent (container ba31424b-6bd4-4c81-8e69-2c33f5889305) has been running since 2026-05-31T19:44:52Z but has emitted zero BRC messages — no heartbeats, no CONSENSUS_PROPOSE. BRC history for the refiner is completely empty. Both reviewer agents (reviewer_refine, reviewer_agent_design) are WAITING_FOR_EVENT blocked on CONSENSUS_PROPOSE since ~19:45:42Z. The orchestrator status endpoint (pipeline_unreachable) is also unresponsive, which may have masked this stall from automated detection. Elapsed: 192s vs stall threshold of 180s.

Recommended action:
Inspect or restart the refiner container (ba31424b-6bd4-4c81-8e69-2c33f5889305). Also investigate why the orchestrator status endpoint at http://orchestrator.egg-system.svc.cluster.local:9849 is unreachable, as this prevented automatic stall detection. Reviewer agents can remain running — they will respond once a valid CONSENSUS_PROPOSE arrives.

````yaml
id: 9a91ac5d-0a34-4c
phase: refine
````

### [2026-05-31T19:48:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 568334dd-bf04-44
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:42.472740+00:00'
````

### [2026-05-31T19:49:24Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: e077395b-90d5-49
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:57.622574+00:00'
````

### [2026-05-31T19:49:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 34725b48-a65c-4e
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:42.472740+00:00'
````

### [2026-05-31T19:50:24Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 32efabd7-e55a-42
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:57.622574+00:00'
````

### [2026-05-31T19:50:47Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 91520cad-bc14-4c
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:50:47.685858+00:00'
````

### [2026-05-31T19:51:24Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 25f0fb28-a890-4b
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:45:57.622574+00:00'
````

### [2026-05-31T19:51:47Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: fb0f98dc-61e7-4b
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:50:47.685858+00:00'
````

### [2026-05-31T19:52:24Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Refine-phase analysis for #2908 (BRC consensus deterministic event-pump + durable agent memory). Frames the problem as a structural seam — the agent must volunteer to re-enter a blocking wait between every BRC event — rather than per-model prompt-following behaviour, and shows the lineage of prior fixes (#2323, #2064/#2482, #1995, #2036, #2451) all narrow that seam without removing it. Documents the existing wrapper SSE + egg-orch message wait fallback at orchestrator/consensus_wrapper.py:404-532 that already partly implements the proposed pattern. Evaluates four options (status-quo prompt hardening; the issue's proposed stateless event-pump + durable memory; persistent --resume session; external watchdog) against a seam-removal criterion and recommends Option B with two boundary qualifications surfaced to the operator: WS0 spike outcome on the Qwen route, and WS8 (MCP→CLI collapse) scope. Enumerates 14 runtime primitives the plan will depend on with file:line evidence and execution-context scope for the plan-phase Primitive-Existence and Trust-Boundary audits, including claude -p one-shot mode already used at sandbox/egg_lib/gha_exec.py:101, the writable agent-outputs/ allowlist at shared/egg_restrictions/patterns.py, and the _RECOVERY_SYSTEM_PROMPT template at orchestrator/consensus_wrapper.py:64-99. Registers four cq-style HITL decisions (WS8 scope, Qwen rollout strategy, safety-budget terminal state, legacy-path retention) and two open-ended feedback questions (operator budget caps, memory artifact retention policy). Skips the six implementation-strategy decisions enumerated in the issue body itself — those are explicitly for the planner/coder against spike data. Complexity assessed as high.

````yaml
id: 2ca40e22-b367-4f
phase: refine
metadata:
  payload:
    summary: "Refine-phase analysis for #2908 (BRC consensus deterministic event-pump\
      \ + durable agent memory). Frames the problem as a structural seam \u2014 the\
      \ agent must volunteer to re-enter a blocking wait between every BRC event \u2014\
      \ rather than per-model prompt-following behaviour, and shows the lineage of\
      \ prior fixes (#2323, #2064/#2482, #1995, #2036, #2451) all narrow that seam\
      \ without removing it. Documents the existing wrapper SSE + egg-orch message\
      \ wait fallback at orchestrator/consensus_wrapper.py:404-532 that already partly\
      \ implements the proposed pattern. Evaluates four options (status-quo prompt\
      \ hardening; the issue's proposed stateless event-pump + durable memory; persistent\
      \ --resume session; external watchdog) against a seam-removal criterion and\
      \ recommends Option B with two boundary qualifications surfaced to the operator:\
      \ WS0 spike outcome on the Qwen route, and WS8 (MCP\u2192CLI collapse) scope.\
      \ Enumerates 14 runtime primitives the plan will depend on with file:line evidence\
      \ and execution-context scope for the plan-phase Primitive-Existence and Trust-Boundary\
      \ audits, including claude -p one-shot mode already used at sandbox/egg_lib/gha_exec.py:101,\
      \ the writable agent-outputs/ allowlist at shared/egg_restrictions/patterns.py,\
      \ and the _RECOVERY_SYSTEM_PROMPT template at orchestrator/consensus_wrapper.py:64-99.\
      \ Registers four cq-style HITL decisions (WS8 scope, Qwen rollout strategy,\
      \ safety-budget terminal state, legacy-path retention) and two open-ended feedback\
      \ questions (operator budget caps, memory artifact retention policy). Skips\
      \ the six implementation-strategy decisions enumerated in the issue body itself\
      \ \u2014 those are explicitly for the planner/coder against spike data. Complexity\
      \ assessed as high."
    attestation: {}
    artifacts:
    - .egg-state/drafts/2908-analysis.md
    risk_considered: ''
    commit_sha: 27d7933e0
    files_changed:
    - .egg-state/drafts/2908-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 1
  commit_sha: 27d7933e0
````

### [2026-05-31T19:52:24Z] reviewer_refine (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: e3393746-287c-43
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T19:52:24Z] reviewer_agent_design (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: b518042c-ebad-43
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T19:52:53Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 89df22a6-8a49-42
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:52:29.975004+00:00'
````

### [2026-05-31T19:53:45Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 11aa55b5-cb45-4b
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:52:29.975004+00:00'
````

### [2026-05-31T19:54:45Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 5c56e92d-1397-49
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:52:29.975004+00:00'
````

### [2026-05-31T19:55:45Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 8e299eba-ccf0-4f
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:52:29.975004+00:00'
````

### [2026-05-31T19:56:29Z] reviewer_agent_design → refiner (CONSENSUS_NACK): NACK from reviewer_agent_design for refiner


Agent-design review of `.egg-state/drafts/2908-analysis.md` (commit 27d7933e0, v1).
Scope: agent-mode-design anti-patterns only — correctness/scope/security covered by reviewer_refine.

### Blocking

1. **2908-analysis.md:29, 134, 182, 288 — the central primitive `claude -p` is the EGG100-flagged anti-pattern, and worse, it would *reverse* an already-correct existing pattern.** The draft repeats throughout that the wrapper "invokes `claude -p` against a warm pod" (line 29), "uses SDK-native one-shot `claude -p`" (line 134), and lists it as a runtime primitive ("`claude -p` SDK one-shot mode", line 288) citing `sandbox/egg_lib/gha_exec.py:101` as the precedent.

   Three problems compound here:

   a. **`claude -p` / `claude --print` is documented in `docs/guides/agent-mode-design.md:90-104` as "not an approved invocation path"** and enforced by the `EGG100` linter (`scripts/check-docker-and-claude-invocations.py`). The implementation checklist in the same doc (line 345) lists "Am I using `claude --print` as a subprocess instead of `build_agent_command()`? (Flagged by EGG100)" as a "Yes is a signal to reconsider" anti-pattern. Two approved primitives exist: `egg_agent.client.run_agent()` (in-pod, in-process) and `build_agent_command()` (orchestrator-spawned containers, producing `python3 -m egg_agent ...`).

   b. **The existing `consensus_wrapper.py` already gets this right.** `orchestrator/consensus_wrapper.py:748-759` builds the agent command as `["python3", "-m", "egg_agent", "--model", model, "--max-turns", str(max_turns)]` with the explicit code comment "Uses the Agent SDK entry point instead of the claude CLI." The proposal as written would *replace* the correct SDK entry point with the EGG100-flagged subprocess — a regression of an architectural decision already enforced in the file the proposal is rewriting. The draft does not acknowledge this; it treats `claude -p` as if it were the natural default.

   c. **The `gha_exec.py:101` citation in the primitive table (line 288) misrepresents the precedent.** That file is *the* documented EGG100 exception for a constrained CI entry point: line 99 carries `# noqa: EGG100 - GHA exec entry point for one-shot prompts`, and lines 88-92 explicitly state "Long-running agents (e.g. overseer) should use the Agent SDK via build_agent_command() instead of claude --print." A BRC consensus reviewer is precisely the long-running, multi-turn, tool-using agent that comment is steering *away* from `--print` — citing it as supporting evidence for the proposed pattern inverts the file's intent and will mislead the planner.

   **Functional consequence (not just lint hygiene):** consensus agents must read files (review code), run git (sync/log/diff), call MCP/CLI (BRC ack/nack/propose, peer artifact reads), and commit. The Agent SDK pathway (`python3 -m egg_agent`) wires MCP, tool permissions, role-scoping (`EGG_AGENT_ROLE`), and the tool-interceptor checks (`tool_interceptor.check_file_write_permission`, cited in the draft's own primitive table at line 298) into one process. Switching to `claude --print` would bypass that integration and require either ad-hoc `--allowed-tools` / `--mcp-config` re-plumbing per invocation or a fleet of `# noqa: EGG100` suppressions. Neither is acknowledged in the draft.

   **Fix:** Replace every `claude -p` reference in the recommended approach (lines 29, 134, 182, 274-281), the workstream descriptions (WS0/WS1/WS6 implicitly), and the primitive table (line 288) with the SDK entry point — concretely `python3 -m egg_agent` per `build_agent_command()` (orchestrator-side) or `run_agent()` (in-pod). The stateless event-pump *shape* (wrapper owns the wait, per-event one-shot agent invocation, durable memory carries continuity) is fully achievable with the SDK — each `python3 -m egg_agent` invocation is already one-shot (it exits when the agent reaches its stop state). The pillar "No new harness" then reads correctly: the proposal converges on the *already-existing* one-shot SDK entry point that the wrapper template uses today, rather than introducing the `--print` subprocess form. Update the `gha_exec.py:101` citation to either remove it (it's not a relevant precedent) or recharacterise it explicitly as the documented EGG100 exception so the planner doesn't repeat the misreading.

### Non-blocking

- **2908-analysis.md:50-54, 269-281 — wrapper-as-event-pump precedent claim is accurate but worth tightening.** The draft asserts the SSE event-pump (`consensus_wrapper.py:404-504`) "is already in production for the confirmed-and-waiting path; the proposal generalises it to the whole BRC lifecycle." From an agent-design lens this is the strongest argument *for* the design — it's an existing approved control pattern, not a novel one. Keep this framing; if the planner ends up sliced across multiple PRs, the first slice that "generalises the existing SSE loop" is the lowest-risk anchor and is the natural place to land the `claude -p`→`python3 -m egg_agent` correction at the same time.

- **2908-analysis.md:191-193, 311-319 — "delta-only re-analysis" needs to be explicit about content vs metadata.** The draft says the agent gets "prior assessment (memory) + `changed_artifacts`/version delta" and "agents don't re-read the codebase/changes/docs each event." Agent-mode-design.md distinguishes "lightweight metadata, task context, and small summaries that orient the agent" (fine) from "baking in large diffs, file contents, or comment threads" (anti-pattern). If `changed_artifacts` is a metadata list (file paths + commit SHA) and the agent fetches its own diffs via git tools, that's the right shape. If the wrapper materialises an inline diff blob into the per-event prompt, that's the pre-fetching anti-pattern. The draft is silent on which it is. Surface this distinction explicitly in the Constraints or Recommended Approach so the planner doesn't default to baking diffs into prompts to "save tokens" — a per-pod warm working tree + git tools is the cheaper and more flexible choice. Non-blocking because it's a planner decision, but it's the kind of decision where the draft framing tilts the planner toward the wrong default.

- **2908-analysis.md:199-202, 311-319 (Open decision #1) — memory-file shape decision should anchor on the "orient, don't constrain" axis, not just on cache friendliness.** The draft frames the append-only vs rewrite/distill trade-off entirely around cache behaviour ("cache-friendly, grows, needs compaction" vs "bounded, busts memory-cache each event"). The agent-mode-design lens adds a second axis: an unbounded append-only log eventually starts *constraining* what the per-event agent can attend to (sea-of-context problem), while a distilled memory keeps "small summaries that orient" as the target shape. Worth surfacing this dimension alongside the cache axis in the Open Decisions list so the planner picks the shape against both criteria, not just the token-cost one.

Agent-design verdict: Option B (the recommended approach) is structurally sound and aligns with egg's existing one-shot agent model — but only when the per-event invocation primitive is corrected to the Agent SDK entry point. The current draft would mislead the planner into building the right control flow around the wrong subprocess primitive, in a way that contradicts both `docs/guides/agent-mode-design.md` and the existing `consensus_wrapper.py`'s already-correct implementation.


````yaml
id: 1eb02a07-fb7b-41
phase: refine
metadata:
  payload:
    reason: "\nAgent-design review of `.egg-state/drafts/2908-analysis.md` (commit\
      \ 27d7933e0, v1).\nScope: agent-mode-design anti-patterns only \u2014 correctness/scope/security\
      \ covered by reviewer_refine.\n\n### Blocking\n\n1. **2908-analysis.md:29, 134,\
      \ 182, 288 \u2014 the central primitive `claude -p` is the EGG100-flagged anti-pattern,\
      \ and worse, it would *reverse* an already-correct existing pattern.** The draft\
      \ repeats throughout that the wrapper \"invokes `claude -p` against a warm pod\"\
      \ (line 29), \"uses SDK-native one-shot `claude -p`\" (line 134), and lists\
      \ it as a runtime primitive (\"`claude -p` SDK one-shot mode\", line 288) citing\
      \ `sandbox/egg_lib/gha_exec.py:101` as the precedent.\n\n   Three problems compound\
      \ here:\n\n   a. **`claude -p` / `claude --print` is documented in `docs/guides/agent-mode-design.md:90-104`\
      \ as \"not an approved invocation path\"** and enforced by the `EGG100` linter\
      \ (`scripts/check-docker-and-claude-invocations.py`). The implementation checklist\
      \ in the same doc (line 345) lists \"Am I using `claude --print` as a subprocess\
      \ instead of `build_agent_command()`? (Flagged by EGG100)\" as a \"Yes is a\
      \ signal to reconsider\" anti-pattern. Two approved primitives exist: `egg_agent.client.run_agent()`\
      \ (in-pod, in-process) and `build_agent_command()` (orchestrator-spawned containers,\
      \ producing `python3 -m egg_agent ...`).\n\n   b. **The existing `consensus_wrapper.py`\
      \ already gets this right.** `orchestrator/consensus_wrapper.py:748-759` builds\
      \ the agent command as `[\"python3\", \"-m\", \"egg_agent\", \"--model\", model,\
      \ \"--max-turns\", str(max_turns)]` with the explicit code comment \"Uses the\
      \ Agent SDK entry point instead of the claude CLI.\" The proposal as written\
      \ would *replace* the correct SDK entry point with the EGG100-flagged subprocess\
      \ \u2014 a regression of an architectural decision already enforced in the file\
      \ the proposal is rewriting. The draft does not acknowledge this; it treats\
      \ `claude -p` as if it were the natural default.\n\n   c. **The `gha_exec.py:101`\
      \ citation in the primitive table (line 288) misrepresents the precedent.**\
      \ That file is *the* documented EGG100 exception for a constrained CI entry\
      \ point: line 99 carries `# noqa: EGG100 - GHA exec entry point for one-shot\
      \ prompts`, and lines 88-92 explicitly state \"Long-running agents (e.g. overseer)\
      \ should use the Agent SDK via build_agent_command() instead of claude --print.\"\
      \ A BRC consensus reviewer is precisely the long-running, multi-turn, tool-using\
      \ agent that comment is steering *away* from `--print` \u2014 citing it as supporting\
      \ evidence for the proposed pattern inverts the file's intent and will mislead\
      \ the planner.\n\n   **Functional consequence (not just lint hygiene):** consensus\
      \ agents must read files (review code), run git (sync/log/diff), call MCP/CLI\
      \ (BRC ack/nack/propose, peer artifact reads), and commit. The Agent SDK pathway\
      \ (`python3 -m egg_agent`) wires MCP, tool permissions, role-scoping (`EGG_AGENT_ROLE`),\
      \ and the tool-interceptor checks (`tool_interceptor.check_file_write_permission`,\
      \ cited in the draft's own primitive table at line 298) into one process. Switching\
      \ to `claude --print` would bypass that integration and require either ad-hoc\
      \ `--allowed-tools` / `--mcp-config` re-plumbing per invocation or a fleet of\
      \ `# noqa: EGG100` suppressions. Neither is acknowledged in the draft.\n\n \
      \  **Fix:** Replace every `claude -p` reference in the recommended approach\
      \ (lines 29, 134, 182, 274-281), the workstream descriptions (WS0/WS1/WS6 implicitly),\
      \ and the primitive table (line 288) with the SDK entry point \u2014 concretely\
      \ `python3 -m egg_agent` per `build_agent_command()` (orchestrator-side) or\
      \ `run_agent()` (in-pod). The stateless event-pump *shape* (wrapper owns the\
      \ wait, per-event one-shot agent invocation, durable memory carries continuity)\
      \ is fully achievable with the SDK \u2014 each `python3 -m egg_agent` invocation\
      \ is already one-shot (it exits when the agent reaches its stop state). The\
      \ pillar \"No new harness\" then reads correctly: the proposal converges on\
      \ the *already-existing* one-shot SDK entry point that the wrapper template\
      \ uses today, rather than introducing the `--print` subprocess form. Update\
      \ the `gha_exec.py:101` citation to either remove it (it's not a relevant precedent)\
      \ or recharacterise it explicitly as the documented EGG100 exception so the\
      \ planner doesn't repeat the misreading.\n\n### Non-blocking\n\n- **2908-analysis.md:50-54,\
      \ 269-281 \u2014 wrapper-as-event-pump precedent claim is accurate but worth\
      \ tightening.** The draft asserts the SSE event-pump (`consensus_wrapper.py:404-504`)\
      \ \"is already in production for the confirmed-and-waiting path; the proposal\
      \ generalises it to the whole BRC lifecycle.\" From an agent-design lens this\
      \ is the strongest argument *for* the design \u2014 it's an existing approved\
      \ control pattern, not a novel one. Keep this framing; if the planner ends up\
      \ sliced across multiple PRs, the first slice that \"generalises the existing\
      \ SSE loop\" is the lowest-risk anchor and is the natural place to land the\
      \ `claude -p`\u2192`python3 -m egg_agent` correction at the same time.\n\n-\
      \ **2908-analysis.md:191-193, 311-319 \u2014 \"delta-only re-analysis\" needs\
      \ to be explicit about content vs metadata.** The draft says the agent gets\
      \ \"prior assessment (memory) + `changed_artifacts`/version delta\" and \"agents\
      \ don't re-read the codebase/changes/docs each event.\" Agent-mode-design.md\
      \ distinguishes \"lightweight metadata, task context, and small summaries that\
      \ orient the agent\" (fine) from \"baking in large diffs, file contents, or\
      \ comment threads\" (anti-pattern). If `changed_artifacts` is a metadata list\
      \ (file paths + commit SHA) and the agent fetches its own diffs via git tools,\
      \ that's the right shape. If the wrapper materialises an inline diff blob into\
      \ the per-event prompt, that's the pre-fetching anti-pattern. The draft is silent\
      \ on which it is. Surface this distinction explicitly in the Constraints or\
      \ Recommended Approach so the planner doesn't default to baking diffs into prompts\
      \ to \"save tokens\" \u2014 a per-pod warm working tree + git tools is the cheaper\
      \ and more flexible choice. Non-blocking because it's a planner decision, but\
      \ it's the kind of decision where the draft framing tilts the planner toward\
      \ the wrong default.\n\n- **2908-analysis.md:199-202, 311-319 (Open decision\
      \ #1) \u2014 memory-file shape decision should anchor on the \"orient, don't\
      \ constrain\" axis, not just on cache friendliness.** The draft frames the append-only\
      \ vs rewrite/distill trade-off entirely around cache behaviour (\"cache-friendly,\
      \ grows, needs compaction\" vs \"bounded, busts memory-cache each event\").\
      \ The agent-mode-design lens adds a second axis: an unbounded append-only log\
      \ eventually starts *constraining* what the per-event agent can attend to (sea-of-context\
      \ problem), while a distilled memory keeps \"small summaries that orient\" as\
      \ the target shape. Worth surfacing this dimension alongside the cache axis\
      \ in the Open Decisions list so the planner picks the shape against both criteria,\
      \ not just the token-cost one.\n\nAgent-design verdict: Option B (the recommended\
      \ approach) is structurally sound and aligns with egg's existing one-shot agent\
      \ model \u2014 but only when the per-event invocation primitive is corrected\
      \ to the Agent SDK entry point. The current draft would mislead the planner\
      \ into building the right control flow around the wrong subprocess primitive,\
      \ in a way that contradicts both `docs/guides/agent-mode-design.md` and the\
      \ existing `consensus_wrapper.py`'s already-correct implementation.\n"
    artifact_references:
    - .egg-state/drafts/2908-analysis.md
    nack_version: 1
  reason: "\nAgent-design review of `.egg-state/drafts/2908-analysis.md` (commit 27d7933e0,\
    \ v1).\nScope: agent-mode-design anti-patterns only \u2014 correctness/scope/security\
    \ covered by reviewer_refine.\n\n### Blocking\n\n1. **2908-analysis.md:29, 134,\
    \ 182, 288 \u2014 the central primitive `claude -p` is the EGG100-flagged anti-pattern,\
    \ and worse, it would *reverse* an already-correct existing pattern.** The draft\
    \ repeats throughout that the wrapper \"invokes `claude -p` against a warm pod\"\
    \ (line 29), \"uses SDK-native one-shot `claude -p`\" (line 134), and lists it\
    \ as a runtime primitive (\"`claude -p` SDK one-shot mode\", line 288) citing\
    \ `sandbox/egg_lib/gha_exec.py:101` as the precedent.\n\n   Three problems compound\
    \ here:\n\n   a. **`claude -p` / `claude --print` is documented in `docs/guides/agent-mode-design.md:90-104`\
    \ as \"not an approved invocation path\"** and enforced by the `EGG100` linter\
    \ (`scripts/check-docker-and-claude-invocations.py`). The implementation checklist\
    \ in the same doc (line 345) lists \"Am I using `claude --print` as a subprocess\
    \ instead of `build_agent_command()`? (Flagged by EGG100)\" as a \"Yes is a signal\
    \ to reconsider\" anti-pattern. Two approved primitives exist: `egg_agent.client.run_agent()`\
    \ (in-pod, in-process) and `build_agent_command()` (orchestrator-spawned containers,\
    \ producing `python3 -m egg_agent ...`).\n\n   b. **The existing `consensus_wrapper.py`\
    \ already gets this right.** `orchestrator/consensus_wrapper.py:748-759` builds\
    \ the agent command as `[\"python3\", \"-m\", \"egg_agent\", \"--model\", model,\
    \ \"--max-turns\", str(max_turns)]` with the explicit code comment \"Uses the\
    \ Agent SDK entry point instead of the claude CLI.\" The proposal as written would\
    \ *replace* the correct SDK entry point with the EGG100-flagged subprocess \u2014\
    \ a regression of an architectural decision already enforced in the file the proposal\
    \ is rewriting. The draft does not acknowledge this; it treats `claude -p` as\
    \ if it were the natural default.\n\n   c. **The `gha_exec.py:101` citation in\
    \ the primitive table (line 288) misrepresents the precedent.** That file is *the*\
    \ documented EGG100 exception for a constrained CI entry point: line 99 carries\
    \ `# noqa: EGG100 - GHA exec entry point for one-shot prompts`, and lines 88-92\
    \ explicitly state \"Long-running agents (e.g. overseer) should use the Agent\
    \ SDK via build_agent_command() instead of claude --print.\" A BRC consensus reviewer\
    \ is precisely the long-running, multi-turn, tool-using agent that comment is\
    \ steering *away* from `--print` \u2014 citing it as supporting evidence for the\
    \ proposed pattern inverts the file's intent and will mislead the planner.\n\n\
    \   **Functional consequence (not just lint hygiene):** consensus agents must\
    \ read files (review code), run git (sync/log/diff), call MCP/CLI (BRC ack/nack/propose,\
    \ peer artifact reads), and commit. The Agent SDK pathway (`python3 -m egg_agent`)\
    \ wires MCP, tool permissions, role-scoping (`EGG_AGENT_ROLE`), and the tool-interceptor\
    \ checks (`tool_interceptor.check_file_write_permission`, cited in the draft's\
    \ own primitive table at line 298) into one process. Switching to `claude --print`\
    \ would bypass that integration and require either ad-hoc `--allowed-tools` /\
    \ `--mcp-config` re-plumbing per invocation or a fleet of `# noqa: EGG100` suppressions.\
    \ Neither is acknowledged in the draft.\n\n   **Fix:** Replace every `claude -p`\
    \ reference in the recommended approach (lines 29, 134, 182, 274-281), the workstream\
    \ descriptions (WS0/WS1/WS6 implicitly), and the primitive table (line 288) with\
    \ the SDK entry point \u2014 concretely `python3 -m egg_agent` per `build_agent_command()`\
    \ (orchestrator-side) or `run_agent()` (in-pod). The stateless event-pump *shape*\
    \ (wrapper owns the wait, per-event one-shot agent invocation, durable memory\
    \ carries continuity) is fully achievable with the SDK \u2014 each `python3 -m\
    \ egg_agent` invocation is already one-shot (it exits when the agent reaches its\
    \ stop state). The pillar \"No new harness\" then reads correctly: the proposal\
    \ converges on the *already-existing* one-shot SDK entry point that the wrapper\
    \ template uses today, rather than introducing the `--print` subprocess form.\
    \ Update the `gha_exec.py:101` citation to either remove it (it's not a relevant\
    \ precedent) or recharacterise it explicitly as the documented EGG100 exception\
    \ so the planner doesn't repeat the misreading.\n\n### Non-blocking\n\n- **2908-analysis.md:50-54,\
    \ 269-281 \u2014 wrapper-as-event-pump precedent claim is accurate but worth tightening.**\
    \ The draft asserts the SSE event-pump (`consensus_wrapper.py:404-504`) \"is already\
    \ in production for the confirmed-and-waiting path; the proposal generalises it\
    \ to the whole BRC lifecycle.\" From an agent-design lens this is the strongest\
    \ argument *for* the design \u2014 it's an existing approved control pattern,\
    \ not a novel one. Keep this framing; if the planner ends up sliced across multiple\
    \ PRs, the first slice that \"generalises the existing SSE loop\" is the lowest-risk\
    \ anchor and is the natural place to land the `claude -p`\u2192`python3 -m egg_agent`\
    \ correction at the same time.\n\n- **2908-analysis.md:191-193, 311-319 \u2014\
    \ \"delta-only re-analysis\" needs to be explicit about content vs metadata.**\
    \ The draft says the agent gets \"prior assessment (memory) + `changed_artifacts`/version\
    \ delta\" and \"agents don't re-read the codebase/changes/docs each event.\" Agent-mode-design.md\
    \ distinguishes \"lightweight metadata, task context, and small summaries that\
    \ orient the agent\" (fine) from \"baking in large diffs, file contents, or comment\
    \ threads\" (anti-pattern). If `changed_artifacts` is a metadata list (file paths\
    \ + commit SHA) and the agent fetches its own diffs via git tools, that's the\
    \ right shape. If the wrapper materialises an inline diff blob into the per-event\
    \ prompt, that's the pre-fetching anti-pattern. The draft is silent on which it\
    \ is. Surface this distinction explicitly in the Constraints or Recommended Approach\
    \ so the planner doesn't default to baking diffs into prompts to \"save tokens\"\
    \ \u2014 a per-pod warm working tree + git tools is the cheaper and more flexible\
    \ choice. Non-blocking because it's a planner decision, but it's the kind of decision\
    \ where the draft framing tilts the planner toward the wrong default.\n\n- **2908-analysis.md:199-202,\
    \ 311-319 (Open decision #1) \u2014 memory-file shape decision should anchor on\
    \ the \"orient, don't constrain\" axis, not just on cache friendliness.** The\
    \ draft frames the append-only vs rewrite/distill trade-off entirely around cache\
    \ behaviour (\"cache-friendly, grows, needs compaction\" vs \"bounded, busts memory-cache\
    \ each event\"). The agent-mode-design lens adds a second axis: an unbounded append-only\
    \ log eventually starts *constraining* what the per-event agent can attend to\
    \ (sea-of-context problem), while a distilled memory keeps \"small summaries that\
    \ orient\" as the target shape. Worth surfacing this dimension alongside the cache\
    \ axis in the Open Decisions list so the planner picks the shape against both\
    \ criteria, not just the token-cost one.\n\nAgent-design verdict: Option B (the\
    \ recommended approach) is structurally sound and aligns with egg's existing one-shot\
    \ agent model \u2014 but only when the per-event invocation primitive is corrected\
    \ to the Agent SDK entry point. The current draft would mislead the planner into\
    \ building the right control flow around the wrong subprocess primitive, in a\
    \ way that contradicts both `docs/guides/agent-mode-design.md` and the existing\
    \ `consensus_wrapper.py`'s already-correct implementation.\n"
  revision_count: 1
````

### [2026-05-31T19:56:29Z] refiner (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: a578444d-14fe-44
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T19:56:34Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner


Reviewed `.egg-state/drafts/2908-analysis.md` (398 lines) against the issue body, spot-checked every cited line number against the actual source, and verified HITL registration on `.egg-state/contracts/issue-2908.json`. The draft is approve-quality: section-by-section it meets the refine rubric, and the citations I verified land where the analysis says they do. Section-by-section findings below.

### Problem Statement (lines 5–36)
Crisp framing of the structural seam: "BRC progress currently depends on the model volunteering to re-enter a blocking wait." Names the concrete failure mode (qwen3.7-max at ~30–50 of a 1000-turn budget, line 16), the per-cycle cost ($1, 20 min, line 18), and the full lineage of prior point-fixes (#2323, #2064/#2482, #1995, #2036, #2451) as artifacts of the same root cause. The desired outcome — "no model can stall BRC consensus by exiting between events" as a property of the control flow, not the prompt — is exactly the right framing for a durable fix vs. the per-model patches the lineage chronicles.

### Current Behavior (lines 38–115)
Extensive code archaeology. I verified the following citations against the actual files:
- `MAX_CONSENSUS_RESTARTS = 3` at `consensus_wrapper.py:38` ✓
- `_RECOVERY_SYSTEM_PROMPT` at lines 64–99 ✓ (full template confirmed)
- SSE event-pump at 404–504 with `egg-orch message wait` fallback at 507–532 ✓ (actual code matches; the "already in production for the confirmed-and-waiting case" framing is accurate — this is a real foundation, not a hand-wave)
- Per-restart OVERSEER_ALERT lines 570–585 (#2806) ✓
- `build_consensus_wrapped_command` at line 720, consumed by `concurrent_executor.py:37` and `routes/pipelines.py:2792` ✓ (both call sites confirmed)
- `message_wait_loop()` at line 267 with first-match return (`if resp.get("matched"): return resp_out`) — confirmed in handler at the cited semantic ✓
- `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS = 60.0` at line 47 ✓
- `REFINER_PATTERNS` block opens at line 488, `REVIEWER_REFINE_PATTERNS` at 509, `REVIEWER_AGENT_DESIGN_PATTERNS` at 479 ✓ (all three role allowlists confirmed; `.egg-state/agent-outputs/<role>/` writability claim holds)
- `_build_brc_preamble` at `routes/pipelines.py:12348` ✓
- `cmd_consensus_status` at `orch_cli.py:2783` (for the `consensus status --json` primitive) ✓

The handler-vs-tool layering claim ("28 agent-facing MCP tools wrap a shared handler layer; ~18/28 already have CLI parity") matches what I see in `sandbox/egg_agent_tools/tools/*.py` — the brc/checkpoint/message/phase/progress/sdlc/task modules are thin schema + invoke_handler wrappers over the shared handler layer the CLI also calls. The "stateless-process invariant is already proven for the Anthropic route" framing is properly hedged with the WS7 measurement provenance and explicitly leaves the Qwen route as the WS0 stop/go gate (line 109) — that is the right risk posture.

### Options Considered (lines 159–253)
Four genuinely-different options:
- **A (prompt-hardening)**: correctly identified as per-model patch that does nothing about the seam
- **B (stateless event-pump)**: the recommended one
- **C (`--resume`)**: correctly distinguishes — `--resume` doesn't solve the Qwen exit problem because the model still has to stay engaged across turns; the wrapper still owns the loop either way, so `--resume` reduces to "cheaper continuity for one short thread" — accurate read
- **D (external watchdog)**: correctly diagnosed as "trades one fragile model-driven loop for another fragile recovery loop" — Qwen's failure mode is *precisely* that re-spawn doesn't change behaviour, so this option is structurally a non-fix

Trade-offs are stated plainly without hedging. Option B's "Cons" section (lines 198–212) honestly surfaces the three real risks (memory-curation reliability, Qwen-route cache TTL, WS8 blast radius) and pairs each with a stated mitigation. This is the level of self-criticism the planner needs.

### Constraints (lines 116–155)
Comprehensive. The #2741 prose-bearing-argv constraint (lines 124–129) is correctly tied to WS8's MCP→CLI collapse — if the planner collapses `consensus propose --summary`/`nack --reason` to CLI, prose MUST take stdin/`--reason-file`, not argv. The "no new harness" pillar (lines 134–139) honestly flags the tail-cache-breakpoint vs. work-caching tension — if work-caching becomes required, that conflicts with `claude -p`'s limited prompt-construction surface and the harness rules out the workaround. Prior-fix preservation list (lines 143–147) covers all six durability-relevant tickets. The advisory-seam paragraph (lines 148–155) correctly defers slice shape to the planner.

### Recommended Approach (lines 254–309)
Option B with two boundary qualifications:
1. WS0 spike outcome gates per-route shipping (Anthropic-only vs. per-route flag vs. both)
2. WS8 is the largest discretionary sub-scope and can be split

The runtime-primitives table (lines 286–303) is exactly what the plan phase's Primitive-Existence and Trust-Boundary audits need — every primitive is named with its actual location and whether it exists today or is net-new. The "advisory seam list" framing in the closing paragraph (line 309) correctly cedes slicing to the planner.

### Open Questions / HITL Registration
Verified on the contract:
- `cq-1` through `cq-4` are present with proper `<!-- egg-hitl-decision -->` markers in the draft and matching entries in `.egg-state/contracts/issue-2908.json` (decisions array). All four have option lists ending in "Other (explain in reply)" per the schema.
- `feedback-1` is registered with Q1 (budget caps for WS0 spike) and Q2 (memory-artifact retention policy as authoritative audit vs. ephemeral).

Each question requires the operator, not the planner:
- `cq-1` (WS8 scope) — what's in/out of *this issue's* deliverables. Borderline (it sits near work-decomposition) but the framing is scope-of-issue, which is operator turf.
- `cq-2` (per-route ship policy when Qwen cache is worse) — explicitly contingent on operator product-intent about Qwen route support; planner can't decide.
- `cq-3` (terminal state when safety budget exhausted: HITL vs. silent vs. hard FAIL) — user-visible behavior + ops contract; operator-only.
- `cq-4` (legacy capped-restart path retention duration) — rollout policy; operator-only.
- Q1 (operator budget caps as hard-fail spike conditions) — operator finance/ops policy.
- Q2 (memory artifact retention/audit policy) — operator data-governance call.

None of these are implementation-strategy questions the planner could decide. The draft also correctly **does not** re-register the six "Open decisions" from the issue body (lines 313–319: "(None — those are explicitly implementation-strategy decisions for the planner/coder against measured data)") — that is the right discipline; HITL gates should not duplicate planner-decidable questions.

### Complexity Assessment (lines 386–394)
"high" is correct. The blast radius framing — "deletion-heavy" and "the agent primitive is **not** changed" — accurately bounds the change while flagging the multi-subsystem reach (orchestrator + sandbox + shared + config/litellm + tests).

### Non-blocking observations

- **`gha_exec.py:101` citation in the primitives table (line 288)** — line 101 of `sandbox/egg_lib/gha_exec.py` is `"--print"` inside the command list at lines 98–104 (`["claude", "--dangerously-skip-permissions", "--print", "--verbose", ...]`). The cite is technically correct (`--print` is `-p`'s long form), but a reader who jumps to just line 101 will see one argv element without context. A range citation `gha_exec.py:98–104` (or noting "the `claude --print` invocation") would land cleaner for the planner.

- **Tool-count consistency (lines 76 vs. 300)** — Current Behavior says "11 modules, ~31 tools"; the runtime-primitives table and Recommended Approach narrative refer to "28 agent-facing MCP tools". `grep -c ToolRegistration` across `sandbox/egg_agent_tools/tools/*.py` produced 8+3+1+3+5+7+4 = 31 (matching the Current Behavior count), and the `__init__.py` adds one re-export. The issue body uses 28. Not blocking — the WS8 deletion footprint is "all of them" either way — but the two numbers should reconcile (likely the issue-body 28 omits `_common`/`_registry` helpers or counts a different surface) before the planner cites either figure as authoritative.

- **Memory-artifact placement under `.egg-state/agent-outputs/<role>/`** — correctly identified as within every role's allowlist, but worth noting for the planner that `.egg-state/agent-outputs/` is currently dominated by structured JSON artifacts (e.g. `2769-architect-output.json`, `issue-2777-replan-architect-output.json`). A new `brc-memory.md` file is allowlisted but represents a new convention (markdown rolling memory vs. JSON deliverable); the plan should explicitly call out the retention/cleanup policy that Q2 of feedback-1 is asking for, since `.egg-state/agent-outputs/` is checked into the repo today.

- **WS0 spike scoping** — the spike is named as gating WS7 but the success criterion is given as "cross-invocation cache hits land against the provider cache" without a numeric threshold. The plan phase will need a measurable gate (e.g. `cache_read_input_tokens / input_tokens >= X` averaged over Y consecutive invocations) — surfacing this as a refine constraint rather than waiting for the plan to invent it would tighten the handoff, but it's also a reasonable thing to leave to the planner since it depends on WS7's instrumentation choices. Q1 of feedback-1 partly covers this.

No blocking issues. The analysis is at the depth and accuracy the plan phase needs.


````yaml
id: a72da7f7-5528-44
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/2908-analysis.md
    - .egg-state/contracts/issue-2908.json
    - orchestrator/consensus_wrapper.py
    - sandbox/egg_agent_tools/handlers/message.py
    - shared/egg_restrictions/patterns.py
    - orchestrator/routes/pipelines.py
    - sandbox/egg_lib/orch_cli.py
    - sandbox/egg_lib/gha_exec.py
    - sandbox/egg_agent_tools/tools/brc.py
    reason: "\nReviewed `.egg-state/drafts/2908-analysis.md` (398 lines) against the\
      \ issue body, spot-checked every cited line number against the actual source,\
      \ and verified HITL registration on `.egg-state/contracts/issue-2908.json`.\
      \ The draft is approve-quality: section-by-section it meets the refine rubric,\
      \ and the citations I verified land where the analysis says they do. Section-by-section\
      \ findings below.\n\n### Problem Statement (lines 5\u201336)\nCrisp framing\
      \ of the structural seam: \"BRC progress currently depends on the model volunteering\
      \ to re-enter a blocking wait.\" Names the concrete failure mode (qwen3.7-max\
      \ at ~30\u201350 of a 1000-turn budget, line 16), the per-cycle cost ($1, 20\
      \ min, line 18), and the full lineage of prior point-fixes (#2323, #2064/#2482,\
      \ #1995, #2036, #2451) as artifacts of the same root cause. The desired outcome\
      \ \u2014 \"no model can stall BRC consensus by exiting between events\" as a\
      \ property of the control flow, not the prompt \u2014 is exactly the right framing\
      \ for a durable fix vs. the per-model patches the lineage chronicles.\n\n###\
      \ Current Behavior (lines 38\u2013115)\nExtensive code archaeology. I verified\
      \ the following citations against the actual files:\n- `MAX_CONSENSUS_RESTARTS\
      \ = 3` at `consensus_wrapper.py:38` \u2713\n- `_RECOVERY_SYSTEM_PROMPT` at lines\
      \ 64\u201399 \u2713 (full template confirmed)\n- SSE event-pump at 404\u2013\
      504 with `egg-orch message wait` fallback at 507\u2013532 \u2713 (actual code\
      \ matches; the \"already in production for the confirmed-and-waiting case\"\
      \ framing is accurate \u2014 this is a real foundation, not a hand-wave)\n-\
      \ Per-restart OVERSEER_ALERT lines 570\u2013585 (#2806) \u2713\n- `build_consensus_wrapped_command`\
      \ at line 720, consumed by `concurrent_executor.py:37` and `routes/pipelines.py:2792`\
      \ \u2713 (both call sites confirmed)\n- `message_wait_loop()` at line 267 with\
      \ first-match return (`if resp.get(\"matched\"): return resp_out`) \u2014 confirmed\
      \ in handler at the cited semantic \u2713\n- `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS\
      \ = 60.0` at line 47 \u2713\n- `REFINER_PATTERNS` block opens at line 488, `REVIEWER_REFINE_PATTERNS`\
      \ at 509, `REVIEWER_AGENT_DESIGN_PATTERNS` at 479 \u2713 (all three role allowlists\
      \ confirmed; `.egg-state/agent-outputs/<role>/` writability claim holds)\n-\
      \ `_build_brc_preamble` at `routes/pipelines.py:12348` \u2713\n- `cmd_consensus_status`\
      \ at `orch_cli.py:2783` (for the `consensus status --json` primitive) \u2713\
      \n\nThe handler-vs-tool layering claim (\"28 agent-facing MCP tools wrap a shared\
      \ handler layer; ~18/28 already have CLI parity\") matches what I see in `sandbox/egg_agent_tools/tools/*.py`\
      \ \u2014 the brc/checkpoint/message/phase/progress/sdlc/task modules are thin\
      \ schema + invoke_handler wrappers over the shared handler layer the CLI also\
      \ calls. The \"stateless-process invariant is already proven for the Anthropic\
      \ route\" framing is properly hedged with the WS7 measurement provenance and\
      \ explicitly leaves the Qwen route as the WS0 stop/go gate (line 109) \u2014\
      \ that is the right risk posture.\n\n### Options Considered (lines 159\u2013\
      253)\nFour genuinely-different options:\n- **A (prompt-hardening)**: correctly\
      \ identified as per-model patch that does nothing about the seam\n- **B (stateless\
      \ event-pump)**: the recommended one\n- **C (`--resume`)**: correctly distinguishes\
      \ \u2014 `--resume` doesn't solve the Qwen exit problem because the model still\
      \ has to stay engaged across turns; the wrapper still owns the loop either way,\
      \ so `--resume` reduces to \"cheaper continuity for one short thread\" \u2014\
      \ accurate read\n- **D (external watchdog)**: correctly diagnosed as \"trades\
      \ one fragile model-driven loop for another fragile recovery loop\" \u2014 Qwen's\
      \ failure mode is *precisely* that re-spawn doesn't change behaviour, so this\
      \ option is structurally a non-fix\n\nTrade-offs are stated plainly without\
      \ hedging. Option B's \"Cons\" section (lines 198\u2013212) honestly surfaces\
      \ the three real risks (memory-curation reliability, Qwen-route cache TTL, WS8\
      \ blast radius) and pairs each with a stated mitigation. This is the level of\
      \ self-criticism the planner needs.\n\n### Constraints (lines 116\u2013155)\n\
      Comprehensive. The #2741 prose-bearing-argv constraint (lines 124\u2013129)\
      \ is correctly tied to WS8's MCP\u2192CLI collapse \u2014 if the planner collapses\
      \ `consensus propose --summary`/`nack --reason` to CLI, prose MUST take stdin/`--reason-file`,\
      \ not argv. The \"no new harness\" pillar (lines 134\u2013139) honestly flags\
      \ the tail-cache-breakpoint vs. work-caching tension \u2014 if work-caching\
      \ becomes required, that conflicts with `claude -p`'s limited prompt-construction\
      \ surface and the harness rules out the workaround. Prior-fix preservation list\
      \ (lines 143\u2013147) covers all six durability-relevant tickets. The advisory-seam\
      \ paragraph (lines 148\u2013155) correctly defers slice shape to the planner.\n\
      \n### Recommended Approach (lines 254\u2013309)\nOption B with two boundary\
      \ qualifications:\n1. WS0 spike outcome gates per-route shipping (Anthropic-only\
      \ vs. per-route flag vs. both)\n2. WS8 is the largest discretionary sub-scope\
      \ and can be split\n\nThe runtime-primitives table (lines 286\u2013303) is exactly\
      \ what the plan phase's Primitive-Existence and Trust-Boundary audits need \u2014\
      \ every primitive is named with its actual location and whether it exists today\
      \ or is net-new. The \"advisory seam list\" framing in the closing paragraph\
      \ (line 309) correctly cedes slicing to the planner.\n\n### Open Questions /\
      \ HITL Registration\nVerified on the contract:\n- `cq-1` through `cq-4` are\
      \ present with proper `<!-- egg-hitl-decision -->` markers in the draft and\
      \ matching entries in `.egg-state/contracts/issue-2908.json` (decisions array).\
      \ All four have option lists ending in \"Other (explain in reply)\" per the\
      \ schema.\n- `feedback-1` is registered with Q1 (budget caps for WS0 spike)\
      \ and Q2 (memory-artifact retention policy as authoritative audit vs. ephemeral).\n\
      \nEach question requires the operator, not the planner:\n- `cq-1` (WS8 scope)\
      \ \u2014 what's in/out of *this issue's* deliverables. Borderline (it sits near\
      \ work-decomposition) but the framing is scope-of-issue, which is operator turf.\n\
      - `cq-2` (per-route ship policy when Qwen cache is worse) \u2014 explicitly\
      \ contingent on operator product-intent about Qwen route support; planner can't\
      \ decide.\n- `cq-3` (terminal state when safety budget exhausted: HITL vs. silent\
      \ vs. hard FAIL) \u2014 user-visible behavior + ops contract; operator-only.\n\
      - `cq-4` (legacy capped-restart path retention duration) \u2014 rollout policy;\
      \ operator-only.\n- Q1 (operator budget caps as hard-fail spike conditions)\
      \ \u2014 operator finance/ops policy.\n- Q2 (memory artifact retention/audit\
      \ policy) \u2014 operator data-governance call.\n\nNone of these are implementation-strategy\
      \ questions the planner could decide. The draft also correctly **does not**\
      \ re-register the six \"Open decisions\" from the issue body (lines 313\u2013\
      319: \"(None \u2014 those are explicitly implementation-strategy decisions for\
      \ the planner/coder against measured data)\") \u2014 that is the right discipline;\
      \ HITL gates should not duplicate planner-decidable questions.\n\n### Complexity\
      \ Assessment (lines 386\u2013394)\n\"high\" is correct. The blast radius framing\
      \ \u2014 \"deletion-heavy\" and \"the agent primitive is **not** changed\" \u2014\
      \ accurately bounds the change while flagging the multi-subsystem reach (orchestrator\
      \ + sandbox + shared + config/litellm + tests).\n\n### Non-blocking observations\n\
      \n- **`gha_exec.py:101` citation in the primitives table (line 288)** \u2014\
      \ line 101 of `sandbox/egg_lib/gha_exec.py` is `\"--print\"` inside the command\
      \ list at lines 98\u2013104 (`[\"claude\", \"--dangerously-skip-permissions\"\
      , \"--print\", \"--verbose\", ...]`). The cite is technically correct (`--print`\
      \ is `-p`'s long form), but a reader who jumps to just line 101 will see one\
      \ argv element without context. A range citation `gha_exec.py:98\u2013104` (or\
      \ noting \"the `claude --print` invocation\") would land cleaner for the planner.\n\
      \n- **Tool-count consistency (lines 76 vs. 300)** \u2014 Current Behavior says\
      \ \"11 modules, ~31 tools\"; the runtime-primitives table and Recommended Approach\
      \ narrative refer to \"28 agent-facing MCP tools\". `grep -c ToolRegistration`\
      \ across `sandbox/egg_agent_tools/tools/*.py` produced 8+3+1+3+5+7+4 = 31 (matching\
      \ the Current Behavior count), and the `__init__.py` adds one re-export. The\
      \ issue body uses 28. Not blocking \u2014 the WS8 deletion footprint is \"all\
      \ of them\" either way \u2014 but the two numbers should reconcile (likely the\
      \ issue-body 28 omits `_common`/`_registry` helpers or counts a different surface)\
      \ before the planner cites either figure as authoritative.\n\n- **Memory-artifact\
      \ placement under `.egg-state/agent-outputs/<role>/`** \u2014 correctly identified\
      \ as within every role's allowlist, but worth noting for the planner that `.egg-state/agent-outputs/`\
      \ is currently dominated by structured JSON artifacts (e.g. `2769-architect-output.json`,\
      \ `issue-2777-replan-architect-output.json`). A new `brc-memory.md` file is\
      \ allowlisted but represents a new convention (markdown rolling memory vs. JSON\
      \ deliverable); the plan should explicitly call out the retention/cleanup policy\
      \ that Q2 of feedback-1 is asking for, since `.egg-state/agent-outputs/` is\
      \ checked into the repo today.\n\n- **WS0 spike scoping** \u2014 the spike is\
      \ named as gating WS7 but the success criterion is given as \"cross-invocation\
      \ cache hits land against the provider cache\" without a numeric threshold.\
      \ The plan phase will need a measurable gate (e.g. `cache_read_input_tokens\
      \ / input_tokens >= X` averaged over Y consecutive invocations) \u2014 surfacing\
      \ this as a refine constraint rather than waiting for the plan to invent it\
      \ would tighten the handoff, but it's also a reasonable thing to leave to the\
      \ planner since it depends on WS7's instrumentation choices. Q1 of feedback-1\
      \ partly covers this.\n\nNo blocking issues. The analysis is at the depth and\
      \ accuracy the plan phase needs.\n"
    ack_version: 1
  version: 1
````

### [2026-05-31T19:56:35Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: ad7cf347-c0be-46
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:35.888209+00:00'
````

### [2026-05-31T19:56:42Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine

````yaml
id: b41a1a80-bdc0-44
phase: refine
metadata:
  consensus_reached: false
````

### [2026-05-31T19:56:48Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: 1e6489e1-3d88-49
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:48.721683+00:00'
````

### [2026-05-31T19:57:36Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: 874df295-859e-45
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:35.888209+00:00'
````

### [2026-05-31T19:57:48Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: d8c570d8-e1f5-4b
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:48.721683+00:00'
````

### [2026-05-31T19:58:36Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: 078edfb2-1e9a-44
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:35.888209+00:00'
````

### [2026-05-31T19:58:48Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: 9c355321-e989-44
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:48.721683+00:00'
````

### [2026-05-31T19:59:46Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: f315261d-4b3c-4f
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:35.888209+00:00'
````

### [2026-05-31T19:59:49Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: dc1ee585-dbc6-44
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T19:56:48.721683+00:00'
````

### [2026-05-31T20:00:20Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Re-propose v2 addressing reviewer_agent_design NACK. Blocker: the analysis cited `claude -p` / `claude --print` as the per-event invocation primitive, including a misread of sandbox/egg_lib/gha_exec.py:101 as supporting evidence. The approved primitive is the Agent SDK entry point `python3 -m egg_agent` built by egg_agent.build_agent_command() — already invoked by orchestrator/consensus_wrapper.py:748-759 with the explicit comment "Uses the Agent SDK entry point instead of the claude CLI". claude --print is EGG100-linted per docs/guides/agent-mode-design.md:90-104 and the gha_exec.py:101 noqa is the documented one-shot-CI exception, not a precedent for long-running tool-using BRC agents. Fix is mostly textual: added Problem-Statement "primitive correction" subsection flagging the issue-body terminology and grounding the fix in agent-mode-design.md + the existing wrapper code, replaced every claude -p reference in Constraints / Options B/C / Pod & worktree section / runtime-primitives table with python3 -m egg_agent / build_agent_command() citing consensus_wrapper.py:748-759, and explicitly noted the gha_exec.py noqa as the EGG100 exception. Also addressed both non-blocking findings: added "memory-delta is metadata, not content" constraint (no inlined diffs in per-event prompts; agent fetches via warm working tree + git tools — addresses the "baking in large diffs" anti-pattern), and added planner note on Open Decision #1 surfacing the "orient, don't constrain" axis alongside cache cost. Stateless-event-pump shape unaffected: python3 -m egg_agent is already one-shot (exits at stop state) and wires MCP/tool-permissions/EGG_AGENT_ROLE into a single process. The "no new harness" pillar now reads correctly — proposal converges on the existing SDK entry point rather than introducing a regressing --print subprocess form.

**Adversarial re-review**

**Your v2 review has TWO equal-weight mandates:**

1. **Verify named v1 blockers were addressed** — confirm the producer fixed what you NACK'd.
2. **Audit the delta since your last review (per REVIEWER-SYNC.md: `git log {last_reviewed_commit}..HEAD --not origin/{base_branch} -p` — the commits landed since the version you last verdicted, NOT the whole accumulated proposal surface) as a fresh reviewer** — ignore your v1 NACK history. Read that diff as if you'd never seen the prior version. Apply your lens (security threat-model, concurrency races, contract AC, line-by-line bugs, silent-fallback shapes — whichever your role owns) to the delta itself, not to whether your previous concerns were satisfied. **Mandate 2 is bounded to this delta** — it does NOT ask you to re-traverse the whole accumulated surface from earlier cycles; that work was amortized when you first reviewed those commits.

Both mandates have equal weight. If (1) passes but (2) finds new issues, you NACK. ACK requires both pass.

**The named-blockers anchor is a known trap. Every reviewer lens has a mandate-2 in its own territory** — security has newly-introduced threat surfaces, concurrency has newly-introduced races, contract has newly-introduced AC drift, code has newly-introduced line-by-line bugs. The four issues that escaped PR #2724 to the GitHub bot were all of code-lens shape (`${ANSWER}` as bare Python, deprecated `datetime.utcnow()`, non-atomic write, bare `except: pass`) — the persistent reviewer correctly answered mandate 1 ("did prior issues get fixed? yes") and skipped mandate 2 ("does this delta introduce new issues? actually yes"). The shape generalizes: whatever your lens, this delta can introduce issues your prior NACK didn't name. Watching the producer deliver a targeted fix pulls strongly toward "verify my fix-request landed → ACK." Recognize the pull and do mandate 2 anyway.

**How to execute mandate 2:**

- Read each new hunk as an operator who's about to copy-paste / run / integrate it. Would this code execute as written? Would these docs send a copy-paster down a working path?
- Apply every rubric pass to the new hunks. New issues outside the scope of your prior NACK are blocking; your prior NACK does not bound this re-review.
- **Fresh-reviewer simulation.** Before issuing your v2 verdict, ask: would a reviewer who has only seen this delta (the commits since your last review) with no NACK history ACK this? If you can't argue yes from that diff alone, NACK.
- **External-bot anchor.** Imagine `egg-reviewer[bot]` reads only this delta (the commits since your last review) with no NACK context. What would it flag? Anything it'd flag, you should NACK first.

**Your v2 verdict must enumerate both halves** so mandate 2 doesn't silently disappear from the record:

- (a) Which v1 blockers you verified-fixed (mandate 1).
- (b) What new issues you audited-and-did-not-find (mandate 2). Name the specific shapes you checked — not "reviewed thoroughly," but "checked for silent fallbacks, doc-snippet executability, API-deprecation, atomicity of file writes." If you can't enumerate (b), you haven't done mandate 2.

**Re-reviews are cheap by design.** Your amortized context means the work is "read the delta, apply your rubric, decide" — minutes, not hours. NACK without hesitance; the orchestrator absorbs cycles. Two NACKs on the same producer where the second names new findings is the correct trajectory, not goalpost-moving. The downstream GitHub reviewer should find nothing in this delta. Anything it catches that lives in this cycle's diff is a miss attributable to this re-review.

````yaml
id: 6216b7c0-7f07-42
phase: refine
metadata:
  payload:
    summary: "Re-propose v2 addressing reviewer_agent_design NACK. Blocker: the analysis\
      \ cited `claude -p` / `claude --print` as the per-event invocation primitive,\
      \ including a misread of sandbox/egg_lib/gha_exec.py:101 as supporting evidence.\
      \ The approved primitive is the Agent SDK entry point `python3 -m egg_agent`\
      \ built by egg_agent.build_agent_command() \u2014 already invoked by orchestrator/consensus_wrapper.py:748-759\
      \ with the explicit comment \"Uses the Agent SDK entry point instead of the\
      \ claude CLI\". claude --print is EGG100-linted per docs/guides/agent-mode-design.md:90-104\
      \ and the gha_exec.py:101 noqa is the documented one-shot-CI exception, not\
      \ a precedent for long-running tool-using BRC agents. Fix is mostly textual:\
      \ added Problem-Statement \"primitive correction\" subsection flagging the issue-body\
      \ terminology and grounding the fix in agent-mode-design.md + the existing wrapper\
      \ code, replaced every claude -p reference in Constraints / Options B/C / Pod\
      \ & worktree section / runtime-primitives table with python3 -m egg_agent /\
      \ build_agent_command() citing consensus_wrapper.py:748-759, and explicitly\
      \ noted the gha_exec.py noqa as the EGG100 exception. Also addressed both non-blocking\
      \ findings: added \"memory-delta is metadata, not content\" constraint (no inlined\
      \ diffs in per-event prompts; agent fetches via warm working tree + git tools\
      \ \u2014 addresses the \"baking in large diffs\" anti-pattern), and added planner\
      \ note on Open Decision #1 surfacing the \"orient, don't constrain\" axis alongside\
      \ cache cost. Stateless-event-pump shape unaffected: python3 -m egg_agent is\
      \ already one-shot (exits at stop state) and wires MCP/tool-permissions/EGG_AGENT_ROLE\
      \ into a single process. The \"no new harness\" pillar now reads correctly \u2014\
      \ proposal converges on the existing SDK entry point rather than introducing\
      \ a regressing --print subprocess form."
    attestation: {}
    artifacts:
    - .egg-state/drafts/2908-analysis.md
    risk_considered: ''
    commit_sha: 5515a1299
    files_changed:
    - .egg-state/drafts/2908-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 2
  commit_sha: 5515a1299
````

### [2026-05-31T20:00:20Z] orchestrator → reviewer_refine (CONSENSUS_RE_REVIEW): Re-review required: refiner submitted new proposal v2

Producer refiner has submitted a new proposal (version 2) after withdrawal. Your previous confirmation was on an earlier version. Please re-review and ACK/NACK the new proposal.

**Adversarial re-review**

**Your v2 review has TWO equal-weight mandates:**

1. **Verify named v1 blockers were addressed** — confirm the producer fixed what you NACK'd.
2. **Audit the delta since your last review (`git log 27d7933e0..5515a1299 --not origin/<base> -p` — the commits landed since the version you last verdicted) as a fresh reviewer** — ignore your v1 NACK history. Read that diff as if you'd never seen the prior version. Apply your lens (security threat-model, concurrency races, contract AC, line-by-line bugs, silent-fallback shapes — whichever your role owns) to the delta itself, not to whether your previous concerns were satisfied. **Mandate 2 is bounded to this delta** — it does NOT ask you to re-traverse the whole accumulated surface from earlier cycles; that work was amortized when you first reviewed those commits.

Both mandates have equal weight. If (1) passes but (2) finds new issues, you NACK. ACK requires both pass.

**The named-blockers anchor is a known trap. Every reviewer lens has a mandate-2 in its own territory** — security has newly-introduced threat surfaces, concurrency has newly-introduced races, contract has newly-introduced AC drift, code has newly-introduced line-by-line bugs. The four issues that escaped PR #2724 to the GitHub bot were all of code-lens shape (`${ANSWER}` as bare Python, deprecated `datetime.utcnow()`, non-atomic write, bare `except: pass`) — the persistent reviewer correctly answered mandate 1 ("did prior issues get fixed? yes") and skipped mandate 2 ("does this delta introduce new issues? actually yes"). The shape generalizes: whatever your lens, this delta can introduce issues your prior NACK didn't name. Watching the producer deliver a targeted fix pulls strongly toward "verify my fix-request landed → ACK." Recognize the pull and do mandate 2 anyway.

**How to execute mandate 2:**

- Read each new hunk as an operator who's about to copy-paste / run / integrate it. Would this code execute as written? Would these docs send a copy-paster down a working path?
- Apply every rubric pass to the new hunks. New issues outside the scope of your prior NACK are blocking; your prior NACK does not bound this re-review.
- **Fresh-reviewer simulation.** Before issuing your v2 verdict, ask: would a reviewer who has only seen this delta (`27d7933e0..5515a1299`) with no NACK history ACK this? If you can't argue yes from that diff alone, NACK.
- **External-bot anchor.** Imagine `egg-reviewer[bot]` reads only this delta (`27d7933e0..5515a1299`) with no NACK context. What would it flag? Anything it'd flag, you should NACK first.

**Your v2 verdict must enumerate both halves** so mandate 2 doesn't silently disappear from the record:

- (a) Which v1 blockers you verified-fixed (mandate 1).
- (b) What new issues you audited-and-did-not-find (mandate 2). Name the specific shapes you checked — not "reviewed thoroughly," but "checked for silent fallbacks, doc-snippet executability, API-deprecation, atomicity of file writes." If you can't enumerate (b), you haven't done mandate 2.

**Re-reviews are cheap by design.** Your amortized context means the work is "read the delta, apply your rubric, decide" — minutes, not hours. NACK without hesitance; the orchestrator absorbs cycles. Two NACKs on the same producer where the second names new findings is the correct trajectory, not goalpost-moving. The downstream GitHub reviewer should find nothing in this delta. Anything it catches that lives in this cycle's diff is a miss attributable to this re-review.

````yaml
id: 2bbef8e2-6e60-41
phase: refine
metadata:
  producer_role: refiner
  version: 2
````

### [2026-05-31T20:00:20Z] reviewer_agent_design (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 5056e0a1-06d9-45
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T20:00:20Z] reviewer_refine (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 0d1d0382-b9ad-4e
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T20:00:57Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Re-propose v2 addressing reviewer_agent_design NACK. Blocker: the analysis cited `claude -p` / `claude --print` as the per-event invocation primitive, including a misread of sandbox/egg_lib/gha_exec.py:101 as supporting evidence. The approved primitive is the Agent SDK entry point `python3 -m egg_agent` built by egg_agent.build_agent_command() — already invoked by orchestrator/consensus_wrapper.py:748-759 with the explicit comment "Uses the Agent SDK entry point instead of the claude CLI". claude --print is EGG100-linted per docs/guides/agent-mode-design.md:90-104 and the gha_exec.py:101 noqa is the documented one-shot-CI exception, not a precedent for long-running tool-using BRC agents. Fix is mostly textual: added Problem-Statement "primitive correction" subsection flagging the issue-body terminology and grounding the fix in agent-mode-design.md + the existing wrapper code, replaced every claude -p reference in Constraints / Options B/C / Pod & worktree section / runtime-primitives table with python3 -m egg_agent / build_agent_command() citing consensus_wrapper.py:748-759. Also addressed both non-blocking findings: added "memory-delta is metadata, not content" constraint (no inlined diffs in per-event prompts; agent fetches via warm working tree + git tools), and added planner note on Open Decision #1 surfacing the "orient, don't constrain" axis alongside cache cost. Stateless-event-pump shape unaffected.

**Adversarial re-review**

**Your v3 review has TWO equal-weight mandates:**

1. **Verify named v2 blockers were addressed** — confirm the producer fixed what you NACK'd.
2. **Audit the delta since your last review (per REVIEWER-SYNC.md: `git log {last_reviewed_commit}..HEAD --not origin/{base_branch} -p` — the commits landed since the version you last verdicted, NOT the whole accumulated proposal surface) as a fresh reviewer** — ignore your v2 NACK history. Read that diff as if you'd never seen the prior version. Apply your lens (security threat-model, concurrency races, contract AC, line-by-line bugs, silent-fallback shapes — whichever your role owns) to the delta itself, not to whether your previous concerns were satisfied. **Mandate 2 is bounded to this delta** — it does NOT ask you to re-traverse the whole accumulated surface from earlier cycles; that work was amortized when you first reviewed those commits.

Both mandates have equal weight. If (1) passes but (2) finds new issues, you NACK. ACK requires both pass.

**The named-blockers anchor is a known trap. Every reviewer lens has a mandate-2 in its own territory** — security has newly-introduced threat surfaces, concurrency has newly-introduced races, contract has newly-introduced AC drift, code has newly-introduced line-by-line bugs. The four issues that escaped PR #2724 to the GitHub bot were all of code-lens shape (`${ANSWER}` as bare Python, deprecated `datetime.utcnow()`, non-atomic write, bare `except: pass`) — the persistent reviewer correctly answered mandate 1 ("did prior issues get fixed? yes") and skipped mandate 2 ("does this delta introduce new issues? actually yes"). The shape generalizes: whatever your lens, this delta can introduce issues your prior NACK didn't name. Watching the producer deliver a targeted fix pulls strongly toward "verify my fix-request landed → ACK." Recognize the pull and do mandate 2 anyway.

**How to execute mandate 2:**

- Read each new hunk as an operator who's about to copy-paste / run / integrate it. Would this code execute as written? Would these docs send a copy-paster down a working path?
- Apply every rubric pass to the new hunks. New issues outside the scope of your prior NACK are blocking; your prior NACK does not bound this re-review.
- **Fresh-reviewer simulation.** Before issuing your v3 verdict, ask: would a reviewer who has only seen this delta (the commits since your last review) with no NACK history ACK this? If you can't argue yes from that diff alone, NACK.
- **External-bot anchor.** Imagine `egg-reviewer[bot]` reads only this delta (the commits since your last review) with no NACK context. What would it flag? Anything it'd flag, you should NACK first.

**Your v3 verdict must enumerate both halves** so mandate 2 doesn't silently disappear from the record:

- (a) Which v2 blockers you verified-fixed (mandate 1).
- (b) What new issues you audited-and-did-not-find (mandate 2). Name the specific shapes you checked — not "reviewed thoroughly," but "checked for silent fallbacks, doc-snippet executability, API-deprecation, atomicity of file writes." If you can't enumerate (b), you haven't done mandate 2.

**Re-reviews are cheap by design.** Your amortized context means the work is "read the delta, apply your rubric, decide" — minutes, not hours. NACK without hesitance; the orchestrator absorbs cycles. Two NACKs on the same producer where the second names new findings is the correct trajectory, not goalpost-moving. The downstream GitHub reviewer should find nothing in this delta. Anything it catches that lives in this cycle's diff is a miss attributable to this re-review.

````yaml
id: 69333939-1c41-46
phase: refine
metadata:
  payload:
    summary: "Re-propose v2 addressing reviewer_agent_design NACK. Blocker: the analysis\
      \ cited `claude -p` / `claude --print` as the per-event invocation primitive,\
      \ including a misread of sandbox/egg_lib/gha_exec.py:101 as supporting evidence.\
      \ The approved primitive is the Agent SDK entry point `python3 -m egg_agent`\
      \ built by egg_agent.build_agent_command() \u2014 already invoked by orchestrator/consensus_wrapper.py:748-759\
      \ with the explicit comment \"Uses the Agent SDK entry point instead of the\
      \ claude CLI\". claude --print is EGG100-linted per docs/guides/agent-mode-design.md:90-104\
      \ and the gha_exec.py:101 noqa is the documented one-shot-CI exception, not\
      \ a precedent for long-running tool-using BRC agents. Fix is mostly textual:\
      \ added Problem-Statement \"primitive correction\" subsection flagging the issue-body\
      \ terminology and grounding the fix in agent-mode-design.md + the existing wrapper\
      \ code, replaced every claude -p reference in Constraints / Options B/C / Pod\
      \ & worktree section / runtime-primitives table with python3 -m egg_agent /\
      \ build_agent_command() citing consensus_wrapper.py:748-759. Also addressed\
      \ both non-blocking findings: added \"memory-delta is metadata, not content\"\
      \ constraint (no inlined diffs in per-event prompts; agent fetches via warm\
      \ working tree + git tools), and added planner note on Open Decision #1 surfacing\
      \ the \"orient, don't constrain\" axis alongside cache cost. Stateless-event-pump\
      \ shape unaffected."
    attestation: {}
    artifacts:
    - .egg-state/drafts/2908-analysis.md
    risk_considered: ''
    commit_sha: 5515a1299
    files_changed:
    - .egg-state/drafts/2908-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 3
  commit_sha: 5515a1299
````

### [2026-05-31T20:00:57Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Re-propose v2 addressing reviewer_agent_design NACK. Blocker: the analysis cited `claude -p` / `claude --print` as the per-event invocation primitive, including a misread of sandbox/egg_lib/gha_exec.py:101 as supporting evidence. The approved primitive is the Agent SDK entry point `python3 -m egg_agent` built by egg_agent.build_agent_command() — already invoked by orchestrator/consensus_wrapper.py:748-759 with the explicit comment "Uses the Agent SDK entry point instead of the claude CLI". claude --print is EGG100-linted per docs/guides/agent-mode-design.md:90-104 and the gha_exec.py:101 noqa is the documented one-shot-CI exception, not a precedent for long-running tool-using BRC agents. Fix is mostly textual: added Problem-Statement "primitive correction" subsection flagging the issue-body terminology and grounding the fix in agent-mode-design.md + the existing wrapper code, replaced every claude -p reference in Constraints / Options B/C / Pod & worktree section / runtime-primitives table with python3 -m egg_agent / build_agent_command() citing consensus_wrapper.py:748-759, and explicitly noted the gha_exec.py noqa as the EGG100 exception. Also addressed both non-blocking findings: added "memory-delta is metadata, not content" constraint (no inlined diffs in per-event prompts; agent fetches via warm working tree + git tools — addresses the "baking in large diffs" anti-pattern), and added planner note on Open Decision #1 surfacing the "orient, don't constrain" axis alongside cache cost. Stateless-event-pump shape unaffected: python3 -m egg_agent is already one-shot (exits at stop state) and wires MCP/tool-permissions/EGG_AGENT_ROLE into a single process. The "no new harness" pillar now reads correctly — proposal converges on the existing SDK entry point rather than introducing a regressing --print subprocess form.

**Adversarial re-review**

**Your v4 review has TWO equal-weight mandates:**

1. **Verify named v3 blockers were addressed** — confirm the producer fixed what you NACK'd.
2. **Audit the delta since your last review (per REVIEWER-SYNC.md: `git log {last_reviewed_commit}..HEAD --not origin/{base_branch} -p` — the commits landed since the version you last verdicted, NOT the whole accumulated proposal surface) as a fresh reviewer** — ignore your v3 NACK history. Read that diff as if you'd never seen the prior version. Apply your lens (security threat-model, concurrency races, contract AC, line-by-line bugs, silent-fallback shapes — whichever your role owns) to the delta itself, not to whether your previous concerns were satisfied. **Mandate 2 is bounded to this delta** — it does NOT ask you to re-traverse the whole accumulated surface from earlier cycles; that work was amortized when you first reviewed those commits.

Both mandates have equal weight. If (1) passes but (2) finds new issues, you NACK. ACK requires both pass.

**The named-blockers anchor is a known trap. Every reviewer lens has a mandate-2 in its own territory** — security has newly-introduced threat surfaces, concurrency has newly-introduced races, contract has newly-introduced AC drift, code has newly-introduced line-by-line bugs. The four issues that escaped PR #2724 to the GitHub bot were all of code-lens shape (`${ANSWER}` as bare Python, deprecated `datetime.utcnow()`, non-atomic write, bare `except: pass`) — the persistent reviewer correctly answered mandate 1 ("did prior issues get fixed? yes") and skipped mandate 2 ("does this delta introduce new issues? actually yes"). The shape generalizes: whatever your lens, this delta can introduce issues your prior NACK didn't name. Watching the producer deliver a targeted fix pulls strongly toward "verify my fix-request landed → ACK." Recognize the pull and do mandate 2 anyway.

**How to execute mandate 2:**

- Read each new hunk as an operator who's about to copy-paste / run / integrate it. Would this code execute as written? Would these docs send a copy-paster down a working path?
- Apply every rubric pass to the new hunks. New issues outside the scope of your prior NACK are blocking; your prior NACK does not bound this re-review.
- **Fresh-reviewer simulation.** Before issuing your v4 verdict, ask: would a reviewer who has only seen this delta (the commits since your last review) with no NACK history ACK this? If you can't argue yes from that diff alone, NACK.
- **External-bot anchor.** Imagine `egg-reviewer[bot]` reads only this delta (the commits since your last review) with no NACK context. What would it flag? Anything it'd flag, you should NACK first.

**Your v4 verdict must enumerate both halves** so mandate 2 doesn't silently disappear from the record:

- (a) Which v3 blockers you verified-fixed (mandate 1).
- (b) What new issues you audited-and-did-not-find (mandate 2). Name the specific shapes you checked — not "reviewed thoroughly," but "checked for silent fallbacks, doc-snippet executability, API-deprecation, atomicity of file writes." If you can't enumerate (b), you haven't done mandate 2.

**Re-reviews are cheap by design.** Your amortized context means the work is "read the delta, apply your rubric, decide" — minutes, not hours. NACK without hesitance; the orchestrator absorbs cycles. Two NACKs on the same producer where the second names new findings is the correct trajectory, not goalpost-moving. The downstream GitHub reviewer should find nothing in this delta. Anything it catches that lives in this cycle's diff is a miss attributable to this re-review.

````yaml
id: 6f8b98c8-7019-4a
phase: refine
metadata:
  payload:
    summary: "Re-propose v2 addressing reviewer_agent_design NACK. Blocker: the analysis\
      \ cited `claude -p` / `claude --print` as the per-event invocation primitive,\
      \ including a misread of sandbox/egg_lib/gha_exec.py:101 as supporting evidence.\
      \ The approved primitive is the Agent SDK entry point `python3 -m egg_agent`\
      \ built by egg_agent.build_agent_command() \u2014 already invoked by orchestrator/consensus_wrapper.py:748-759\
      \ with the explicit comment \"Uses the Agent SDK entry point instead of the\
      \ claude CLI\". claude --print is EGG100-linted per docs/guides/agent-mode-design.md:90-104\
      \ and the gha_exec.py:101 noqa is the documented one-shot-CI exception, not\
      \ a precedent for long-running tool-using BRC agents. Fix is mostly textual:\
      \ added Problem-Statement \"primitive correction\" subsection flagging the issue-body\
      \ terminology and grounding the fix in agent-mode-design.md + the existing wrapper\
      \ code, replaced every claude -p reference in Constraints / Options B/C / Pod\
      \ & worktree section / runtime-primitives table with python3 -m egg_agent /\
      \ build_agent_command() citing consensus_wrapper.py:748-759, and explicitly\
      \ noted the gha_exec.py noqa as the EGG100 exception. Also addressed both non-blocking\
      \ findings: added \"memory-delta is metadata, not content\" constraint (no inlined\
      \ diffs in per-event prompts; agent fetches via warm working tree + git tools\
      \ \u2014 addresses the \"baking in large diffs\" anti-pattern), and added planner\
      \ note on Open Decision #1 surfacing the \"orient, don't constrain\" axis alongside\
      \ cache cost. Stateless-event-pump shape unaffected: python3 -m egg_agent is\
      \ already one-shot (exits at stop state) and wires MCP/tool-permissions/EGG_AGENT_ROLE\
      \ into a single process. The \"no new harness\" pillar now reads correctly \u2014\
      \ proposal converges on the existing SDK entry point rather than introducing\
      \ a regressing --print subprocess form."
    attestation: {}
    artifacts:
    - .egg-state/drafts/2908-analysis.md
    risk_considered: ''
    commit_sha: 5515a1299
    files_changed:
    - .egg-state/drafts/2908-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 4
  commit_sha: 5515a1299
````

### [2026-05-31T20:01:27Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 71285621-70c3-4a
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T20:01:03.519691+00:00'
````

### [2026-05-31T20:01:27Z] refiner (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 2a8a903c-469a-4f
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T20:02:06Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 9fed9b4b-3994-4d
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T20:02:05.996443+00:00'
````

### [2026-05-31T20:03:03Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner


Agent-design re-review of `.egg-state/drafts/2908-analysis.md` at v4 (commit 5515a1299). The artifact and commit_sha are identical across v2/v3/v4 (the producer re-proposed three times against the same commit, presumably to handle a stale-version race elsewhere); the textual delta I reviewed below is the only change since v1 (commit 27d7933e0).

Delta vs v1: +70/-22 lines in `.egg-state/drafts/2908-analysis.md` only; no other files touched.

### (a) Mandate 1 — v1 blocker verified-fixed

The v1 blocker was the `claude -p` primitive throughout the analysis (problem statement, constraints, both option descriptions, primitive table) plus the misread of `sandbox/egg_lib/gha_exec.py:101` as a precedent. Verified each fix in the delta:

- **Problem Statement (lines 29-35)** — `claude -p` replaced with "the agent against a warm pod **via the Agent SDK entry point** (`python3 -m egg_agent`, built by `egg_agent.build_agent_command()` — the same primitive `orchestrator/consensus_wrapper.py:748–759` already uses today)". Grounds the fix in the existing wrapper's code.
- **New Primitive-correction subsection (lines 41-58)** — explicitly flags the issue-body terminology error, cites `docs/guides/agent-mode-design.md:90–104` and the EGG100 lint, characterises `gha_exec.py:101` as the documented one-shot-CI `# noqa` exception (citing the lines-88-92 docstring that steers long-running tool-using agents toward `build_agent_command()`), and explains that the stateless-event-pump shape is preserved because `python3 -m egg_agent` is already one-shot. This is the structural correction I asked for; future planner reads will not repeat the misread.
- **Pod & worktree section (lines 118-120)** — `claude -p` → `python3 -m egg_agent`.
- **Gateway-enforced role boundaries constraint (lines 142-150)** — `claude -p` → `python3 -m egg_agent`, plus a new sentence noting the SDK entry point wires MCP / tool permissions / `tool_interceptor.check_file_write_permission` into the same process so no per-event `--allowed-tools` / `--mcp-config` re-plumbing is needed. Strengthens the agent-design argument.
- **"No new harness" constraint (lines 160-167)** — `claude -p` → `build_agent_command()` / `python3 -m egg_agent` with the explicit clarifier that each invocation "is itself one-shot — it exits when the agent reaches its stop state."
- **Option B Approach (lines 217-220)** — `claude -p` → "Agent SDK entry point (`python3 -m egg_agent` from `build_agent_command()`)".
- **Option C Approach (lines 255-258)** — `claude --resume` reframed as "the SDK's `--resume` continuation"; `claude -p` → "fresh `python3 -m egg_agent`".
- **Primitive table (line 327)** — old `claude -p SDK one-shot mode | sandbox (already used in sandbox/egg_lib/gha_exec.py:101) | in-sandbox-agent` row replaced with `Agent SDK entry point python3 -m egg_agent (built by egg_agent.build_agent_command()) | shared/egg_agent/; **already invoked by orchestrator/consensus_wrapper.py:748–759**` and an explicit "**NOT** `claude --print` (EGG100 anti-pattern …); the `gha_exec.py:101` `claude --print` call is the documented one-shot-CI exception and is **not** a precedent for long-running tool-using agents." This is the strongest possible planner-facing correction.

Both v1 non-blocking points also addressed:
- **"Delta-only re-analysis" content-vs-metadata distinction** — new Constraint added at lines 168-177: *"Memory-delta is metadata, not content"* explicitly stating the per-event prompt receives `prior assessment summary + a metadata delta (changed file paths, commit SHAs, version markers, NACK reasons)` and **not** inlined diff/file-content blobs, with the agent fetching its own diffs via the warm working tree + git tools; cites the agent-mode-design "baking in large diffs" anti-pattern directly. Removes the planner-defaults-to-pre-fetching risk.
- **Open Decision #1 (memory shape)** — new planner note at lines 358-365 surfaces the "orient, don't constrain" axis alongside cache behaviour: "an unbounded append-only memory eventually starts *constraining* what the per-event agent can attend to (sea-of-context), while a distilled memory keeps 'small summaries that orient' as the target shape."

### (b) Mandate 2 — fresh-reviewer audit of the delta

Read the delta as if I'd never seen v1, applying my agent-mode-design rubric to the new hunks themselves (not to whether my v1 NACK landed). Specific shapes I checked in the new content:

- **New `claude --print` / `claude -p` references introduced by the delta** — none; delta removes all of them. The new prose names `claude --print` only in the corrective citations (Primitive-correction subsection, primitive-table row), which is the correct way to mention it.
- **New direct Anthropic-API / `httpx` / Anthropic SDK calls introduced** — none. The wrapper continues to invoke the Agent SDK entry point (`python3 -m egg_agent`), so EGG200 is unaffected.
- **New pre-fetching anti-patterns introduced** — none. The new "Memory-delta is metadata, not content" constraint actively *prevents* this anti-pattern; the new planner note on Open Decision #1 nudges the memory-file shape away from unbounded-context drift.
- **New structured-output-for-humans patterns introduced** — none. BRC ACK/NACK remains free-form prose.
- **New post-processing pipelines (script parses agent output to take action) introduced** — none. The wrapper drives the event loop deterministically; the agent acts directly via tools / CLI / MCP, no parsing layer in between.
- **New rigid micromanaged procedures introduced** — none. The new "Primitive correction" subsection and "Memory-delta is metadata" constraint specify *what* (use the SDK entry point; don't bake content into prompts) rather than *how*, and the planner note explicitly leaves the memory-shape decision open with two evaluation axes.
- **New prompt-level security shapes introduced** — none. The gateway / `tool_interceptor.check_file_write_permission` enforcement remains the boundary; the delta's new sentence (lines 145-150) explicitly notes the SDK entry point continues to wire these in per-invocation.
- **New hardcoded model identifiers (EGG201) introduced** — none. The delta does not name any models.
- **New action-surface or memory-file write paths outside the existing allowlist** — none. The memory file (`.egg-state/agent-outputs/<role>/brc-memory.md`) is in the role allowlist per `shared/egg_restrictions/patterns.py:479-514` (already established in the unchanged Current Behavior section), and the delta does not introduce new write locations.
- **Doc-snippet executability** — the new prose contains no code samples; only file path + line citations, which I spot-checked (`agent-mode-design.md:90-104`, `consensus_wrapper.py:748-759`, `gha_exec.py:101` + `gha_exec.py:88-92`) and they are accurate.
- **External-bot anchor** — imagined `egg-reviewer[bot]` reading only the v1→v2 delta with no NACK context: it would scan for `claude --print` regressions (none — delta removes them), direct API calls (none), pinned model IDs (none), pre-fetching of diff content (the delta actively bans this), and prompt-level constraints masquerading as security (none). Nothing flagged.

Fresh-reviewer simulation: a reviewer with only this delta and no NACK history would ACK — the prose is a textual correction that aligns the analysis with documented conventions, adds two explicit constraints that prevent agent-design anti-patterns downstream, and supplements a planner-facing decision with an additional evaluation axis. No new infrastructure, no new code, no new anti-patterns.

Agent-design verdict: ACK at v4. The analysis is now structurally sound for the planner from the agent-mode-design lens — the per-event invocation primitive is correctly named, the memory-delta shape is constrained against the pre-fetching anti-pattern, and the memory-file-shape Open Decision is anchored on both cache and "orient, don't constrain" axes.


````yaml
id: 98f96ad0-6bc7-40
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/2908-analysis.md
    reason: "\nAgent-design re-review of `.egg-state/drafts/2908-analysis.md` at v4\
      \ (commit 5515a1299). The artifact and commit_sha are identical across v2/v3/v4\
      \ (the producer re-proposed three times against the same commit, presumably\
      \ to handle a stale-version race elsewhere); the textual delta I reviewed below\
      \ is the only change since v1 (commit 27d7933e0).\n\nDelta vs v1: +70/-22 lines\
      \ in `.egg-state/drafts/2908-analysis.md` only; no other files touched.\n\n\
      ### (a) Mandate 1 \u2014 v1 blocker verified-fixed\n\nThe v1 blocker was the\
      \ `claude -p` primitive throughout the analysis (problem statement, constraints,\
      \ both option descriptions, primitive table) plus the misread of `sandbox/egg_lib/gha_exec.py:101`\
      \ as a precedent. Verified each fix in the delta:\n\n- **Problem Statement (lines\
      \ 29-35)** \u2014 `claude -p` replaced with \"the agent against a warm pod **via\
      \ the Agent SDK entry point** (`python3 -m egg_agent`, built by `egg_agent.build_agent_command()`\
      \ \u2014 the same primitive `orchestrator/consensus_wrapper.py:748\u2013759`\
      \ already uses today)\". Grounds the fix in the existing wrapper's code.\n-\
      \ **New Primitive-correction subsection (lines 41-58)** \u2014 explicitly flags\
      \ the issue-body terminology error, cites `docs/guides/agent-mode-design.md:90\u2013\
      104` and the EGG100 lint, characterises `gha_exec.py:101` as the documented\
      \ one-shot-CI `# noqa` exception (citing the lines-88-92 docstring that steers\
      \ long-running tool-using agents toward `build_agent_command()`), and explains\
      \ that the stateless-event-pump shape is preserved because `python3 -m egg_agent`\
      \ is already one-shot. This is the structural correction I asked for; future\
      \ planner reads will not repeat the misread.\n- **Pod & worktree section (lines\
      \ 118-120)** \u2014 `claude -p` \u2192 `python3 -m egg_agent`.\n- **Gateway-enforced\
      \ role boundaries constraint (lines 142-150)** \u2014 `claude -p` \u2192 `python3\
      \ -m egg_agent`, plus a new sentence noting the SDK entry point wires MCP /\
      \ tool permissions / `tool_interceptor.check_file_write_permission` into the\
      \ same process so no per-event `--allowed-tools` / `--mcp-config` re-plumbing\
      \ is needed. Strengthens the agent-design argument.\n- **\"No new harness\"\
      \ constraint (lines 160-167)** \u2014 `claude -p` \u2192 `build_agent_command()`\
      \ / `python3 -m egg_agent` with the explicit clarifier that each invocation\
      \ \"is itself one-shot \u2014 it exits when the agent reaches its stop state.\"\
      \n- **Option B Approach (lines 217-220)** \u2014 `claude -p` \u2192 \"Agent\
      \ SDK entry point (`python3 -m egg_agent` from `build_agent_command()`)\".\n\
      - **Option C Approach (lines 255-258)** \u2014 `claude --resume` reframed as\
      \ \"the SDK's `--resume` continuation\"; `claude -p` \u2192 \"fresh `python3\
      \ -m egg_agent`\".\n- **Primitive table (line 327)** \u2014 old `claude -p SDK\
      \ one-shot mode | sandbox (already used in sandbox/egg_lib/gha_exec.py:101)\
      \ | in-sandbox-agent` row replaced with `Agent SDK entry point python3 -m egg_agent\
      \ (built by egg_agent.build_agent_command()) | shared/egg_agent/; **already\
      \ invoked by orchestrator/consensus_wrapper.py:748\u2013759**` and an explicit\
      \ \"**NOT** `claude --print` (EGG100 anti-pattern \u2026); the `gha_exec.py:101`\
      \ `claude --print` call is the documented one-shot-CI exception and is **not**\
      \ a precedent for long-running tool-using agents.\" This is the strongest possible\
      \ planner-facing correction.\n\nBoth v1 non-blocking points also addressed:\n\
      - **\"Delta-only re-analysis\" content-vs-metadata distinction** \u2014 new\
      \ Constraint added at lines 168-177: *\"Memory-delta is metadata, not content\"\
      * explicitly stating the per-event prompt receives `prior assessment summary\
      \ + a metadata delta (changed file paths, commit SHAs, version markers, NACK\
      \ reasons)` and **not** inlined diff/file-content blobs, with the agent fetching\
      \ its own diffs via the warm working tree + git tools; cites the agent-mode-design\
      \ \"baking in large diffs\" anti-pattern directly. Removes the planner-defaults-to-pre-fetching\
      \ risk.\n- **Open Decision #1 (memory shape)** \u2014 new planner note at lines\
      \ 358-365 surfaces the \"orient, don't constrain\" axis alongside cache behaviour:\
      \ \"an unbounded append-only memory eventually starts *constraining* what the\
      \ per-event agent can attend to (sea-of-context), while a distilled memory keeps\
      \ 'small summaries that orient' as the target shape.\"\n\n### (b) Mandate 2\
      \ \u2014 fresh-reviewer audit of the delta\n\nRead the delta as if I'd never\
      \ seen v1, applying my agent-mode-design rubric to the new hunks themselves\
      \ (not to whether my v1 NACK landed). Specific shapes I checked in the new content:\n\
      \n- **New `claude --print` / `claude -p` references introduced by the delta**\
      \ \u2014 none; delta removes all of them. The new prose names `claude --print`\
      \ only in the corrective citations (Primitive-correction subsection, primitive-table\
      \ row), which is the correct way to mention it.\n- **New direct Anthropic-API\
      \ / `httpx` / Anthropic SDK calls introduced** \u2014 none. The wrapper continues\
      \ to invoke the Agent SDK entry point (`python3 -m egg_agent`), so EGG200 is\
      \ unaffected.\n- **New pre-fetching anti-patterns introduced** \u2014 none.\
      \ The new \"Memory-delta is metadata, not content\" constraint actively *prevents*\
      \ this anti-pattern; the new planner note on Open Decision #1 nudges the memory-file\
      \ shape away from unbounded-context drift.\n- **New structured-output-for-humans\
      \ patterns introduced** \u2014 none. BRC ACK/NACK remains free-form prose.\n\
      - **New post-processing pipelines (script parses agent output to take action)\
      \ introduced** \u2014 none. The wrapper drives the event loop deterministically;\
      \ the agent acts directly via tools / CLI / MCP, no parsing layer in between.\n\
      - **New rigid micromanaged procedures introduced** \u2014 none. The new \"Primitive\
      \ correction\" subsection and \"Memory-delta is metadata\" constraint specify\
      \ *what* (use the SDK entry point; don't bake content into prompts) rather than\
      \ *how*, and the planner note explicitly leaves the memory-shape decision open\
      \ with two evaluation axes.\n- **New prompt-level security shapes introduced**\
      \ \u2014 none. The gateway / `tool_interceptor.check_file_write_permission`\
      \ enforcement remains the boundary; the delta's new sentence (lines 145-150)\
      \ explicitly notes the SDK entry point continues to wire these in per-invocation.\n\
      - **New hardcoded model identifiers (EGG201) introduced** \u2014 none. The delta\
      \ does not name any models.\n- **New action-surface or memory-file write paths\
      \ outside the existing allowlist** \u2014 none. The memory file (`.egg-state/agent-outputs/<role>/brc-memory.md`)\
      \ is in the role allowlist per `shared/egg_restrictions/patterns.py:479-514`\
      \ (already established in the unchanged Current Behavior section), and the delta\
      \ does not introduce new write locations.\n- **Doc-snippet executability** \u2014\
      \ the new prose contains no code samples; only file path + line citations, which\
      \ I spot-checked (`agent-mode-design.md:90-104`, `consensus_wrapper.py:748-759`,\
      \ `gha_exec.py:101` + `gha_exec.py:88-92`) and they are accurate.\n- **External-bot\
      \ anchor** \u2014 imagined `egg-reviewer[bot]` reading only the v1\u2192v2 delta\
      \ with no NACK context: it would scan for `claude --print` regressions (none\
      \ \u2014 delta removes them), direct API calls (none), pinned model IDs (none),\
      \ pre-fetching of diff content (the delta actively bans this), and prompt-level\
      \ constraints masquerading as security (none). Nothing flagged.\n\nFresh-reviewer\
      \ simulation: a reviewer with only this delta and no NACK history would ACK\
      \ \u2014 the prose is a textual correction that aligns the analysis with documented\
      \ conventions, adds two explicit constraints that prevent agent-design anti-patterns\
      \ downstream, and supplements a planner-facing decision with an additional evaluation\
      \ axis. No new infrastructure, no new code, no new anti-patterns.\n\nAgent-design\
      \ verdict: ACK at v4. The analysis is now structurally sound for the planner\
      \ from the agent-mode-design lens \u2014 the per-event invocation primitive\
      \ is correctly named, the memory-delta shape is constrained against the pre-fetching\
      \ anti-pattern, and the memory-file-shape Open Decision is anchored on both\
      \ cache and \"orient, don't constrain\" axes.\n"
    ack_version: 4
  version: 4
````

### [2026-05-31T20:03:03Z] refiner (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 2b864990-7268-42
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T20:03:07Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner (pending_acks)

Producer refiner cannot confirm: not fully ACKed. Pending reviewers: ['reviewer_refine']

````yaml
id: 721bdd7c-aee2-4c
phase: refine
metadata:
  pending_acks: true
````

### [2026-05-31T20:03:08Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: 42f4874c-eb71-46
phase: refine
metadata:
  consensus_reached: false
````

### [2026-05-31T20:03:13Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: 847c7744-7838-4c
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T20:03:13.724746+00:00'
````

### [2026-05-31T20:03:13Z] reviewer_agent_design (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 223f2a65-ce95-43
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T20:03:14Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: b2212a30-d884-42
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T20:03:14.532007+00:00'
````

### [2026-05-31T20:03:22Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: 817c03d8-1f92-4e
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-31T20:03:22.868792+00:00'
````

### [2026-05-31T20:03:53Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner


Re-ACK at v4 (artifact unchanged from v2 — same commit SHA `5515a1299`; producer re-proposed v3/v4 without artifact churn while my v2 ACK was being assembled, so my review still applies in full). Repeating the v2-ACK substance below so the audit log is self-contained at v4.

### (a) Mandate 1 — verify named v1 blockers
Zero blockers from me on v1 (unconditional ACK with three non-blocking nits). Mandate 1 vacuous for my edge. Adjudicating the `reviewer_agent_design` NACK addressed in the v1→v2 commit message ("address agent-design NACK") is that reviewer's job — and that edge has already ACKed and confirmed at 20:03:08 per the message log.

### (b) Mandate 2 — adversarial audit of `27d7933e0..5515a1299` as a fresh reviewer
Delta is +70/-22 in `.egg-state/drafts/2908-analysis.md`. Specific shapes checked against the new hunks:

1. **Factual accuracy of every new file:line citation** — all verified against the actual source:
   - `docs/guides/agent-mode-design.md:90–104` says exactly what the analysis claims: line 90 section header "Use the Agent SDK, not claude --print or direct API calls"; line 92 "must not invoke `claude --print` as a subprocess"; line 102 "`claude --print` is not an approved invocation path"; line 104 "Enforced by: `EGG200` linter ...; `EGG100` linter (`claude --print` subprocess calls)". The "EGG100-linted anti-pattern" framing is correct.
   - `sandbox/egg_lib/gha_exec.py:88–92` does steer long-running tool-using agents to `build_agent_command()`: "Long-running agents (e.g. overseer) should use the Agent SDK via build_agent_command() instead of claude --print."
   - `sandbox/egg_lib/gha_exec.py:101` is inside the `["claude", "--dangerously-skip-permissions", "--print", ...]` argv at lines 98–104; the `noqa: EGG100` comment sits on the `"claude"` element at line 99. The cite lands on the correct construct.
   - `orchestrator/consensus_wrapper.py:748–759` (cited as the existing precedent) literally builds `["python3", "-m", "egg_agent", "--model", model, "--max-turns", str(max_turns)]` at lines 750–758, with the function comment at line 749 reading "Uses the Agent SDK entry point instead of the claude CLI." The "already uses today" framing is exactly true.
   - `egg_agent.build_agent_command()` exists at `shared/egg_agent/command.py:11`, exported from `shared/egg_agent/__init__.py:8,14`; the module docstring confirms it builds the `python3 -m egg_agent` command.
   - `shared/egg_agent/__main__.py` is the SDK entry point at lines 5–7.

2. **Internal consistency of the `claude -p` → `python3 -m egg_agent` swap** — grepped the post-edit doc. Surviving `claude -p` / `claude --print` references appear only in the new "Primitive correction" paragraph (lines 42–43) and the primitives-table row (line 327), both deliberately naming the anti-pattern to disclaim it. No stale `claude -p` references in unchanged prose. Problem Statement, Current Behavior (Pod & worktree isolation), Constraints (Gateway-enforced role boundaries, No new harness), Option B Approach, Option C Approach, and the primitives table all consistently use `python3 -m egg_agent` post-edit.

3. **HITL contract drift since v1** — Re-checked `mcp__sdlc__show_contract` after the v2 sync: `cq-1` through `cq-4` and `feedback-1` (Q1/Q2) unchanged; no new HITL questions, no new resolutions. The new "Planner note on Open Decision #1 (memory shape)" (line 360) is correctly framed as a planner cross-axis orientation note, not as an operator-only question that would require contract registration. That's the right scoping for a planner-territory decision.

4. **Open Decision #1 cross-reference** — issue body item #1 reads "Memory location/schema/**shape** — append-only (cache-friendly, grows, needs compaction) vs rewrite/distill (bounded, busts memory-cache each event)". The analysis's "append-only vs rewrite/distill against cache behaviour only" + added "orient, don't constrain" axis is a faithful summary plus a legitimate planner hint.

5. **New "Memory-delta is metadata, not content" constraint** (lines 160–172) — explicitly rules out inlining diff blobs / file-contents snapshots into per-event prompts; rules in working-tree + git tools fetching by the agent. This is a substantive improvement that materially helps the planner avoid the "baking in large diffs" anti-pattern and links it to the memory-shape Open Decision #1. No factual issues with the new constraint.

6. **Anti-pattern propagation through Recommendation** — Option B's Pros section "Converges consensus execution toward egg's existing one-shot agent model; net deletion of the wait machinery" is *strengthened* by the v2 correction because `python3 -m egg_agent` is the actual SDK entry point the orchestrator spawns today, while `claude -p` (the v1 wording) was an anti-pattern that would have regressed an existing architectural decision.

7. **Doc-snippet executability / copy-paste hazards** — analysis is prose; no runnable snippets in v1 base or v2 delta. The primitives-table row contains shell-like syntax (`python3 -m egg_agent`) but as a primitive name, not as a copy-pasteable command. No operator hazard.

8. **Constraint re-anchoring** — "No change to the agent primitive" (line 142) and "Gateway-enforced role boundaries persist per-invocation" (lines 143–149) both gain credibility under the v2 correction: `python3 -m egg_agent` wires MCP, `tool_interceptor.check_file_write_permission`, and `EGG_AGENT_ROLE` scoping into the same process automatically — exactly the per-invocation invariant the constraint asserts. The v1 wording on this point was misleading; v2 is correct.

### Verdict
ACK v4 (artifact = v2; commit `5515a1299`). Mandate 1 vacuous for my edge. Mandate 2 enumerated and found zero new issues — every new cite verifies, swap is consistent doc-wide, no HITL drift, no anti-patterns introduced, new "Memory-delta is metadata" constraint is a substantive plus.

### Non-blocking observations
- v1 nits I flagged (tool-count 28-vs-31, `gha_exec.py:101` cite precision, `.egg-state/agent-outputs/<role>/` retention policy surfacing) remain unaddressed but non-blocking. The new primitives-table row incidentally mitigates the `gha_exec.py:101` cite-precision nit by giving it surrounding context.
- The v2 correction implicitly identifies a documentation issue in the **issue body** itself (#2908 body says "SDK-native one-shot `claude -p`" which is the anti-pattern). The analysis correctly handles this in-document; flagging for the operator that the issue body should be updated to align is an out-of-band concern, not an analysis-doc blocker.


````yaml
id: d1b2d053-ecbb-49
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/2908-analysis.md
    - docs/guides/agent-mode-design.md
    - orchestrator/consensus_wrapper.py
    - shared/egg_agent/__init__.py
    - shared/egg_agent/command.py
    - shared/egg_agent/__main__.py
    - sandbox/egg_lib/gha_exec.py
    - .egg-state/contracts/issue-2908.json
    reason: "\nRe-ACK at v4 (artifact unchanged from v2 \u2014 same commit SHA `5515a1299`;\
      \ producer re-proposed v3/v4 without artifact churn while my v2 ACK was being\
      \ assembled, so my review still applies in full). Repeating the v2-ACK substance\
      \ below so the audit log is self-contained at v4.\n\n### (a) Mandate 1 \u2014\
      \ verify named v1 blockers\nZero blockers from me on v1 (unconditional ACK with\
      \ three non-blocking nits). Mandate 1 vacuous for my edge. Adjudicating the\
      \ `reviewer_agent_design` NACK addressed in the v1\u2192v2 commit message (\"\
      address agent-design NACK\") is that reviewer's job \u2014 and that edge has\
      \ already ACKed and confirmed at 20:03:08 per the message log.\n\n### (b) Mandate\
      \ 2 \u2014 adversarial audit of `27d7933e0..5515a1299` as a fresh reviewer\n\
      Delta is +70/-22 in `.egg-state/drafts/2908-analysis.md`. Specific shapes checked\
      \ against the new hunks:\n\n1. **Factual accuracy of every new file:line citation**\
      \ \u2014 all verified against the actual source:\n   - `docs/guides/agent-mode-design.md:90\u2013\
      104` says exactly what the analysis claims: line 90 section header \"Use the\
      \ Agent SDK, not claude --print or direct API calls\"; line 92 \"must not invoke\
      \ `claude --print` as a subprocess\"; line 102 \"`claude --print` is not an\
      \ approved invocation path\"; line 104 \"Enforced by: `EGG200` linter ...; `EGG100`\
      \ linter (`claude --print` subprocess calls)\". The \"EGG100-linted anti-pattern\"\
      \ framing is correct.\n   - `sandbox/egg_lib/gha_exec.py:88\u201392` does steer\
      \ long-running tool-using agents to `build_agent_command()`: \"Long-running\
      \ agents (e.g. overseer) should use the Agent SDK via build_agent_command()\
      \ instead of claude --print.\"\n   - `sandbox/egg_lib/gha_exec.py:101` is inside\
      \ the `[\"claude\", \"--dangerously-skip-permissions\", \"--print\", ...]` argv\
      \ at lines 98\u2013104; the `noqa: EGG100` comment sits on the `\"claude\"`\
      \ element at line 99. The cite lands on the correct construct.\n   - `orchestrator/consensus_wrapper.py:748\u2013\
      759` (cited as the existing precedent) literally builds `[\"python3\", \"-m\"\
      , \"egg_agent\", \"--model\", model, \"--max-turns\", str(max_turns)]` at lines\
      \ 750\u2013758, with the function comment at line 749 reading \"Uses the Agent\
      \ SDK entry point instead of the claude CLI.\" The \"already uses today\" framing\
      \ is exactly true.\n   - `egg_agent.build_agent_command()` exists at `shared/egg_agent/command.py:11`,\
      \ exported from `shared/egg_agent/__init__.py:8,14`; the module docstring confirms\
      \ it builds the `python3 -m egg_agent` command.\n   - `shared/egg_agent/__main__.py`\
      \ is the SDK entry point at lines 5\u20137.\n\n2. **Internal consistency of\
      \ the `claude -p` \u2192 `python3 -m egg_agent` swap** \u2014 grepped the post-edit\
      \ doc. Surviving `claude -p` / `claude --print` references appear only in the\
      \ new \"Primitive correction\" paragraph (lines 42\u201343) and the primitives-table\
      \ row (line 327), both deliberately naming the anti-pattern to disclaim it.\
      \ No stale `claude -p` references in unchanged prose. Problem Statement, Current\
      \ Behavior (Pod & worktree isolation), Constraints (Gateway-enforced role boundaries,\
      \ No new harness), Option B Approach, Option C Approach, and the primitives\
      \ table all consistently use `python3 -m egg_agent` post-edit.\n\n3. **HITL\
      \ contract drift since v1** \u2014 Re-checked `mcp__sdlc__show_contract` after\
      \ the v2 sync: `cq-1` through `cq-4` and `feedback-1` (Q1/Q2) unchanged; no\
      \ new HITL questions, no new resolutions. The new \"Planner note on Open Decision\
      \ #1 (memory shape)\" (line 360) is correctly framed as a planner cross-axis\
      \ orientation note, not as an operator-only question that would require contract\
      \ registration. That's the right scoping for a planner-territory decision.\n\
      \n4. **Open Decision #1 cross-reference** \u2014 issue body item #1 reads \"\
      Memory location/schema/**shape** \u2014 append-only (cache-friendly, grows,\
      \ needs compaction) vs rewrite/distill (bounded, busts memory-cache each event)\"\
      . The analysis's \"append-only vs rewrite/distill against cache behaviour only\"\
      \ + added \"orient, don't constrain\" axis is a faithful summary plus a legitimate\
      \ planner hint.\n\n5. **New \"Memory-delta is metadata, not content\" constraint**\
      \ (lines 160\u2013172) \u2014 explicitly rules out inlining diff blobs / file-contents\
      \ snapshots into per-event prompts; rules in working-tree + git tools fetching\
      \ by the agent. This is a substantive improvement that materially helps the\
      \ planner avoid the \"baking in large diffs\" anti-pattern and links it to the\
      \ memory-shape Open Decision #1. No factual issues with the new constraint.\n\
      \n6. **Anti-pattern propagation through Recommendation** \u2014 Option B's Pros\
      \ section \"Converges consensus execution toward egg's existing one-shot agent\
      \ model; net deletion of the wait machinery\" is *strengthened* by the v2 correction\
      \ because `python3 -m egg_agent` is the actual SDK entry point the orchestrator\
      \ spawns today, while `claude -p` (the v1 wording) was an anti-pattern that\
      \ would have regressed an existing architectural decision.\n\n7. **Doc-snippet\
      \ executability / copy-paste hazards** \u2014 analysis is prose; no runnable\
      \ snippets in v1 base or v2 delta. The primitives-table row contains shell-like\
      \ syntax (`python3 -m egg_agent`) but as a primitive name, not as a copy-pasteable\
      \ command. No operator hazard.\n\n8. **Constraint re-anchoring** \u2014 \"No\
      \ change to the agent primitive\" (line 142) and \"Gateway-enforced role boundaries\
      \ persist per-invocation\" (lines 143\u2013149) both gain credibility under\
      \ the v2 correction: `python3 -m egg_agent` wires MCP, `tool_interceptor.check_file_write_permission`,\
      \ and `EGG_AGENT_ROLE` scoping into the same process automatically \u2014 exactly\
      \ the per-invocation invariant the constraint asserts. The v1 wording on this\
      \ point was misleading; v2 is correct.\n\n### Verdict\nACK v4 (artifact = v2;\
      \ commit `5515a1299`). Mandate 1 vacuous for my edge. Mandate 2 enumerated and\
      \ found zero new issues \u2014 every new cite verifies, swap is consistent doc-wide,\
      \ no HITL drift, no anti-patterns introduced, new \"Memory-delta is metadata\"\
      \ constraint is a substantive plus.\n\n### Non-blocking observations\n- v1 nits\
      \ I flagged (tool-count 28-vs-31, `gha_exec.py:101` cite precision, `.egg-state/agent-outputs/<role>/`\
      \ retention policy surfacing) remain unaddressed but non-blocking. The new primitives-table\
      \ row incidentally mitigates the `gha_exec.py:101` cite-precision nit by giving\
      \ it surrounding context.\n- The v2 correction implicitly identifies a documentation\
      \ issue in the **issue body** itself (#2908 body says \"SDK-native one-shot\
      \ `claude -p`\" which is the anti-pattern). The analysis correctly handles this\
      \ in-document; flagging for the operator that the issue body should be updated\
      \ to align is an out-of-band concern, not an analysis-doc blocker.\n"
    ack_version: 4
  version: 4
````

### [2026-05-31T20:03:53Z] orchestrator → refiner (STATUS): Ready to confirm — all confirm preconditions satisfied

Your proposal (version 4) is ready to confirm — all blocking reviews are clear and global confirm preconditions are met. Run `egg-orch consensus confirmed` to confirm.

````yaml
id: 1107a82e-f5b6-47
phase: refine
metadata:
  ready_to_confirm: true
  version: 4
````

### [2026-05-31T20:03:53Z] refiner (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 84bd402f-2954-4b
phase: refine
metadata:
  state: WORKING
````

### [2026-05-31T20:04:00Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner

````yaml
id: bd8b7a27-d94c-4e
phase: refine
metadata:
  consensus_reached: true
````

### [2026-05-31T20:04:00Z] reviewer_agent_design (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 42b2f84b-5e04-42
phase: refine
metadata:
  state: WORKING
````
