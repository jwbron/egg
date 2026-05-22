# BRC Consensus History — refine phase

Generated: 2026-05-22T01:33:41Z
Pipeline: issue-2769

### [2026-05-22T01:21:36Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: d62abdd5-907c-40
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:22:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 645d0ba8-dd58-4a
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:23:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: a6f2fafc-69bb-43
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:23:26Z] overseer (OVERSEER_ALERT): agent-heartbeat-stall [high]

Refine-phase agents never launched for pipeline issue-2769 — zero executions, zero proposals, zero heartbeats after pipeline start

Detail:
Pipeline issue-2769 is in the refine phase (cycle 0). The contract shows agent_executions=[], BRC history is empty (0 items), and no checkpoints exist. BRC state lists refiner/reviewer_agent_design/reviewer_refine all as WORKING with no proposals. The orchestrator is healthy (uptime ~4600s, state stores OK). The pipeline_unreachable alert from the monitor was a long-poll timeout on /api/v1/pipelines/issue-2769/status — not an orchestrator outage. The real issue is that zero agents have been dispatched. The pipeline may have been registered without triggering agent launch.

Recommended action:
Operator should verify agent dispatch for issue-2769 refine phase. Options: (1) re-trigger the pipeline via egg-orch or the webhook endpoint, (2) manually launch the refiner agent, or (3) inspect the orchestrator logs for dispatch failures around the pipeline registration time (~2026-05-22T00:05Z).

````yaml
id: 171c4e9a-d281-4f
phase: refine
````

### [2026-05-22T01:23:31Z] overseer (HEARTBEAT): heartbeat: WORKING

Monitoring cycle 1 complete. Agent-heartbeat-stall alert issued. Awaiting operator action or agent launch. Running cycle 2.

````yaml
id: d279a9e0-ab4b-47
phase: refine
metadata:
  state: WORKING
````

### [2026-05-22T01:23:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 60cb9cdf-f9df-4d
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:24:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 94bf73b5-822f-4b
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:24:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: eb9703a3-b65a-4c
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:25:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 5c0e083e-642d-4f
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:25:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: ebacc1f1-b02b-40
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:26:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: d91065f3-489e-47
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:26:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 795a976a-2c79-48
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:26:53Z] overseer (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

Overseer monitoring active. Waiting for refiner CONSENSUS_PROPOSE. Cycle 13: refiner 321s elapsed, reviewers healthy. Two self-alerts cleared: progress event emitted, heartbeat refreshed.

````yaml
id: 1e28afe8-40b3-4d
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:20:46.000000+00:00'
````

### [2026-05-22T01:27:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: efb8b822-f7bc-4f
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:27:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: b9d65e35-d77e-46
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:28:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 9687073e-8cc1-4a
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:28:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 29eaa948-53c4-4a
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:29:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 875016a4-19d4-4e
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:29:48Z] overseer (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

Overseer cycle 24: 523s elapsed, all agents running. Refiner WORKING (no propose yet). Reviewers heartbeating normally. Refreshing overseer heartbeat to stay under 120s threshold.

````yaml
id: c9a01a04-6c04-47
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:20:46.000000+00:00'
````

### [2026-05-22T01:29:51Z] reviewer_refine (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: 8c5fd16d-68ff-46
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:21:28.083399+00:00'
````

### [2026-05-22T01:30:03Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE

````yaml
id: d9a33a4b-4697-40
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:23:02.990597+00:00'
````

### [2026-05-22T01:30:42Z] refiner (CONSENSUS_PROPOSE): Proposal from refiner

Analysis draft for #2769 (non-Claude models per agent via LiteLLM proxy). Recommends Option A: gateway-side upstream router keyed by IP-based session metadata, with LiteLLM as a separate Deployment in egg-system, Claude path structurally unchanged, and inert-by-default behavior. Surfaces the Claude Code auto-compaction risk as the primary validation gate and proposes the recognized-alias mitigation. Inventories the runtime primitives the plan phase will depend on (file:line evidence for get_anthropic_client, proxy_anthropic_messages, _inject_anthropic_credentials, _SSEAccumulator, _filter_blocked_tools, session_manager.get_session_by_ip, build_consensus_wrapped_command's hardcoded `model="opus"`, the per-overseer model fields on PipelineConfig, _PROTECTED_ENV_KEYS, the ANTHROPIC_BASE_URL spawn-time injection, and the three sites that bake `opus[1m]`). Registers 11 HITL decisions (cq-1..cq-11) covering LiteLLM topology, routing signal, per-agent model config shape, acceptance-test role, harness choice, target backend, credential handling, failure policy, private-mode tool-strip policy, slice decomposition, and the `[1m]` syntax; plus 5 open-ended feedback questions on Qwen hardware, target role list, swap-out interface, cost tracking, and compliance/data-residency. Complexity assessed as high. No `## Additional Context` was present in the issue body, so nothing is pre-resolved.

````yaml
id: 4bbb1f0c-c54a-45
phase: refine
metadata:
  payload:
    summary: 'Analysis draft for #2769 (non-Claude models per agent via LiteLLM proxy).
      Recommends Option A: gateway-side upstream router keyed by IP-based session
      metadata, with LiteLLM as a separate Deployment in egg-system, Claude path structurally
      unchanged, and inert-by-default behavior. Surfaces the Claude Code auto-compaction
      risk as the primary validation gate and proposes the recognized-alias mitigation.
      Inventories the runtime primitives the plan phase will depend on (file:line
      evidence for get_anthropic_client, proxy_anthropic_messages, _inject_anthropic_credentials,
      _SSEAccumulator, _filter_blocked_tools, session_manager.get_session_by_ip, build_consensus_wrapped_command''s
      hardcoded `model="opus"`, the per-overseer model fields on PipelineConfig, _PROTECTED_ENV_KEYS,
      the ANTHROPIC_BASE_URL spawn-time injection, and the three sites that bake `opus[1m]`).
      Registers 11 HITL decisions (cq-1..cq-11) covering LiteLLM topology, routing
      signal, per-agent model config shape, acceptance-test role, harness choice,
      target backend, credential handling, failure policy, private-mode tool-strip
      policy, slice decomposition, and the `[1m]` syntax; plus 5 open-ended feedback
      questions on Qwen hardware, target role list, swap-out interface, cost tracking,
      and compliance/data-residency. Complexity assessed as high. No `## Additional
      Context` was present in the issue body, so nothing is pre-resolved.'
    attestation: {}
    artifacts:
    - .egg-state/drafts/2769-analysis.md
    risk_considered: "Primary risk surface is Claude Code's model-name-derived auto-compaction\
      \ (raised in the issue and verified via web research on Claude Code's compaction\
      \ docs). Mitigation proposed: present Claude Code a recognized alias and route\
      \ on a separate per-agent signal (session metadata). Secondary risks: LiteLLM\
      \ supply-chain (March 2026 PyPI incident \u2014 addressed via cosign verification\
      \ and a swap-out interface question in feedback), credential/topology choice\
      \ (deferred to HITL), and per-agent config drift between orchestrator-declared\
      \ upstream and agent-passed model (mitigation: spawner derives both from one\
      \ config field)."
    commit_sha: 9dc90338c
    files_changed:
    - .egg-state/drafts/2769-analysis.md
    tests_run: []
    tasks_satisfied: []
  version: 1
  commit_sha: 9dc90338c
````

### [2026-05-22T01:30:42Z] reviewer_agent_design (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 5331ff20-5d25-49
phase: refine
metadata:
  state: WORKING
````

### [2026-05-22T01:30:42Z] reviewer_refine (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: a142d5ea-659a-48
phase: refine
metadata:
  state: WORKING
````

### [2026-05-22T01:31:03Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 16e9ec0b-72ad-44
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:30:47.149619+00:00'
````

### [2026-05-22T01:32:02Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: dc719d78-a30b-49
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:30:47.149619+00:00'
````

### [2026-05-22T01:32:05Z] overseer (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

Overseer cycle 29: BRC review round active. refiner PROPOSED (v1), reviewer_agent_design and reviewer_refine WORKING on proposal. Awaiting ACK/NACK.

````yaml
id: 7bcb2661-22ce-4d
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:20:46.000000+00:00'
````

### [2026-05-22T01:32:31Z] reviewer_agent_design → refiner (CONSENSUS_ACK): ACK from reviewer_agent_design for refiner

Reviewed `.egg-state/drafts/2769-analysis.md` (344 lines) against the agent-mode design rubric in `docs/guides/agent-mode-design.md`. Cross-checked the cited integration points (`gateway/gateway.py:9316-9329`, `:9355`, `:9410`, `:9552`, `:9752`, `:9774`, `:10019`; `orchestrator/consensus_wrapper.py:620-662`; `orchestrator/kubernetes_spawner.py:807`; `shared/egg_agent/client.py:62`; `shared/egg_agent/__main__.py:35`; `sandbox/llm/runner.py:49`) — primitive references and line numbers are accurate.

**No blocking agent-design issues found.** The recommended Option A is structurally consistent with every agent-mode invariant we care about:

1. **EGG200 (no direct LLM calls outside sandbox)** — preserved. The design extends the *existing* gateway proxy (already `noqa: EGG200` as legitimate proxy infrastructure) rather than introducing new `httpx`/Anthropic SDK call sites in orchestrator/shared. LiteLLM is reached *from the gateway*, not from agents or orchestrator code. The constraint "LiteLLM must not be directly reachable from sandbox pods" (line 66) and the Squid-allowlist-exclusion note (line 67) explicitly reinforce this.

2. **EGG201 (model aliases, not pinned IDs)** — explicitly honored: "No pinned model snapshot versions. Use model aliases (e.g. `opus`, `sonnet`, `qwen3-coder-30b`)" (line 69). cq-11 surfaces the `opus[1m]` Claude-only suffix cleanly as a decision rather than silently leaving it baked in.

3. **Agent SDK path preserved** — Option D (bypass Claude Code via `egg_agent.client.run_agent()`) is correctly identified as the SDK path already taking `--model` (line 124), and the recommended Option A retains the existing Claude Code harness for the no-op default. No new harness-bypass code is introduced.

4. **Gateway as the per-request policy point** — Option A routes via per-agent session metadata (lookup-by-IP, same mechanism as `session_mode` today, line 77); the SSE accumulator, tool-strip, and credential injection sit *above* the upstream selection and stay upstream-agnostic. This is exactly the right factoring per the design guide's "minimal intermediation" principle — the gateway adds one indirection (upstream registry) without inserting itself into request semantics.

5. **No pre-fetched content baked into prompts, no JSON-for-humans, no post-processing pipeline, no rigid procedures, no prompt-level security used as a substitute for sandbox enforcement** — none of these anti-patterns appear in the design.

6. **The "present Claude Code a recognized alias while routing on session metadata" mitigation** (lines 79, 84, 138, cq-2 option 1) is a Claude-Code-compatibility workaround, not an agent-design concern: the *agent itself* sees and uses the real backend; only Claude Code's internal compaction-bookkeeping sees the alias. The agent isn't constrained from doing anything it could otherwise do.

7. **Option B rejection (LiteLLM-fronts-everything)** correctly identifies that adding an LLM-translation hop to the Claude path would (a) violate the no-regression constraint and (b) make compaction-math worse, not better. Option C rejection correctly identifies the body-routing conflict with the recognized-alias mitigation. The option analysis is honest.

### Non-blocking
- **cq-11 framing** — option (b) ("hoist into a single config helper that strips `[1m]` when the resolved upstream is non-Claude") is the most agent-design-aligned answer: it keeps model strings backend-agnostic at the API boundary and isolates the Claude-only suffix to a single resolution point. Worth flagging this in the operator's decision context if the producer revises.
- **cq-7 option (c)** ("sandbox sets its own per-agent API key via `extra_env`") is correctly flagged as a "probably non-starter" because it inverts the zero-credential sandbox invariant. Good — this is the right call from an agent-mode-security perspective and should stay weighted against in any plan-phase refinement.
- **The runtime-primitive table (lines 145-172)** is the right shape for handing the plan phase exact anchors; nothing for the design reviewer to flag, just noting it's well-suited to keep the plan agent grounded without baking diffs into its prompt.

````yaml
id: 3b63d219-a510-4b
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/2769-analysis.md
    reason: "Reviewed `.egg-state/drafts/2769-analysis.md` (344 lines) against the\
      \ agent-mode design rubric in `docs/guides/agent-mode-design.md`. Cross-checked\
      \ the cited integration points (`gateway/gateway.py:9316-9329`, `:9355`, `:9410`,\
      \ `:9552`, `:9752`, `:9774`, `:10019`; `orchestrator/consensus_wrapper.py:620-662`;\
      \ `orchestrator/kubernetes_spawner.py:807`; `shared/egg_agent/client.py:62`;\
      \ `shared/egg_agent/__main__.py:35`; `sandbox/llm/runner.py:49`) \u2014 primitive\
      \ references and line numbers are accurate.\n\n**No blocking agent-design issues\
      \ found.** The recommended Option A is structurally consistent with every agent-mode\
      \ invariant we care about:\n\n1. **EGG200 (no direct LLM calls outside sandbox)**\
      \ \u2014 preserved. The design extends the *existing* gateway proxy (already\
      \ `noqa: EGG200` as legitimate proxy infrastructure) rather than introducing\
      \ new `httpx`/Anthropic SDK call sites in orchestrator/shared. LiteLLM is reached\
      \ *from the gateway*, not from agents or orchestrator code. The constraint \"\
      LiteLLM must not be directly reachable from sandbox pods\" (line 66) and the\
      \ Squid-allowlist-exclusion note (line 67) explicitly reinforce this.\n\n2.\
      \ **EGG201 (model aliases, not pinned IDs)** \u2014 explicitly honored: \"No\
      \ pinned model snapshot versions. Use model aliases (e.g. `opus`, `sonnet`,\
      \ `qwen3-coder-30b`)\" (line 69). cq-11 surfaces the `opus[1m]` Claude-only\
      \ suffix cleanly as a decision rather than silently leaving it baked in.\n\n\
      3. **Agent SDK path preserved** \u2014 Option D (bypass Claude Code via `egg_agent.client.run_agent()`)\
      \ is correctly identified as the SDK path already taking `--model` (line 124),\
      \ and the recommended Option A retains the existing Claude Code harness for\
      \ the no-op default. No new harness-bypass code is introduced.\n\n4. **Gateway\
      \ as the per-request policy point** \u2014 Option A routes via per-agent session\
      \ metadata (lookup-by-IP, same mechanism as `session_mode` today, line 77);\
      \ the SSE accumulator, tool-strip, and credential injection sit *above* the\
      \ upstream selection and stay upstream-agnostic. This is exactly the right factoring\
      \ per the design guide's \"minimal intermediation\" principle \u2014 the gateway\
      \ adds one indirection (upstream registry) without inserting itself into request\
      \ semantics.\n\n5. **No pre-fetched content baked into prompts, no JSON-for-humans,\
      \ no post-processing pipeline, no rigid procedures, no prompt-level security\
      \ used as a substitute for sandbox enforcement** \u2014 none of these anti-patterns\
      \ appear in the design.\n\n6. **The \"present Claude Code a recognized alias\
      \ while routing on session metadata\" mitigation** (lines 79, 84, 138, cq-2\
      \ option 1) is a Claude-Code-compatibility workaround, not an agent-design concern:\
      \ the *agent itself* sees and uses the real backend; only Claude Code's internal\
      \ compaction-bookkeeping sees the alias. The agent isn't constrained from doing\
      \ anything it could otherwise do.\n\n7. **Option B rejection (LiteLLM-fronts-everything)**\
      \ correctly identifies that adding an LLM-translation hop to the Claude path\
      \ would (a) violate the no-regression constraint and (b) make compaction-math\
      \ worse, not better. Option C rejection correctly identifies the body-routing\
      \ conflict with the recognized-alias mitigation. The option analysis is honest.\n\
      \n### Non-blocking\n- **cq-11 framing** \u2014 option (b) (\"hoist into a single\
      \ config helper that strips `[1m]` when the resolved upstream is non-Claude\"\
      ) is the most agent-design-aligned answer: it keeps model strings backend-agnostic\
      \ at the API boundary and isolates the Claude-only suffix to a single resolution\
      \ point. Worth flagging this in the operator's decision context if the producer\
      \ revises.\n- **cq-7 option (c)** (\"sandbox sets its own per-agent API key\
      \ via `extra_env`\") is correctly flagged as a \"probably non-starter\" because\
      \ it inverts the zero-credential sandbox invariant. Good \u2014 this is the\
      \ right call from an agent-mode-security perspective and should stay weighted\
      \ against in any plan-phase refinement.\n- **The runtime-primitive table (lines\
      \ 145-172)** is the right shape for handing the plan phase exact anchors; nothing\
      \ for the design reviewer to flag, just noting it's well-suited to keep the\
      \ plan agent grounded without baking diffs into its prompt."
    ack_version: 1
  version: 1
````

### [2026-05-22T01:32:31Z] refiner (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 3f2d1950-7a72-4c
phase: refine
metadata:
  state: WORKING
````

### [2026-05-22T01:32:34Z] reviewer_agent_design (CONSENSUS_CONFIRMED): Confirmed by reviewer_agent_design

````yaml
id: 3c2f19cf-59a4-46
phase: refine
metadata:
  consensus_reached: false
````

### [2026-05-22T01:32:39Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: 2dad4f29-8b4e-43
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:32:39.055879+00:00'
````

### [2026-05-22T01:32:47Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT

````yaml
id: 1536be5b-2cc0-46
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:32:47.754773+00:00'
````

### [2026-05-22T01:33:32Z] reviewer_refine → refiner (CONSENSUS_ACK): ACK from reviewer_refine for refiner


Reviewed the refine analysis at `.egg-state/drafts/2769-analysis.md` (commit 9dc90338c) against all seven review-criteria sections, plus cross-checked every cited file/line against the working tree, plus verified the contract scaffolding produced the expected 11 decisions + 5 feedback questions.

## Section-by-Section Evaluation

### 1. Problem Understanding — strong
The Problem Statement (lines 5-13) correctly extracts all three hard requirements from the issue body (parallel Claude + non-Claude, per-agent independent selectability, no-regression on the Claude path) and names the architectural intent (gateway-as-router, LiteLLM as translation layer). The side-benefit (harness decoupling) and the rejection rationale for `claude-code-router` are preserved. The supply-chain footnote on the March 2026 LiteLLM PyPI incident (line 13) is a useful unprompted add — it surfaces a real risk without overweighting it.

### 2. Research Quality — exemplary
Spot-checked the 22 entries in the Runtime-primitives table against the working tree:
- `get_anthropic_client` cited at gateway.py:9316-9329 — actual definition spans 9317 (singleton decl) to 9329 (return), match.
- `_inject_anthropic_credentials` at :9355 — verified.
- `_filter_blocked_tools` at :9410 — verified.
- `_SSEAccumulator` at :9552 — verified.
- `get_session_by_ip` lookup in `proxy_anthropic_messages` — verified at gateway.py:9775 (draft says 9774, off-by-1).
- `proxy_anthropic_messages` — actual `def` is at gateway.py:9753 (draft says 9752, off-by-1 — the `@app.route` decorator is on the prior line).
- `proxy_count_tokens` — actual at gateway.py:10020 (draft says 10019, same off-by-1).
- `overseer_decision_maker_model` / `overseer_advisor_model` Field decls at orchestrator/models.py:546 and :620 — verified.
- `build_consensus_wrapped_command(model="opus", ...)` at orchestrator/consensus_wrapper.py:620-622 with `--model` at :658 — verified, falls inside the cited 653-662 range.
- Call sites at concurrent_executor.py:454 and routes/pipelines.py:2704 with no model arg — verified, confirming the "every non-overseer agent is opus-only by hardcoding" claim.
- `DEFAULT_MODEL = "opus[1m]"` at shared/egg_agent/client.py:62 — verified.
- `parser.add_argument("--model", default="opus[1m]", ...)` at shared/egg_agent/__main__.py:35 — verified.
- `cmd.extend(["--model", "opus[1m]"])` at sandbox/llm/runner.py:49 — verified.
- `ANTHROPIC_BASE_URL=GATEWAY_K8S_URL` at orchestrator/kubernetes_spawner.py:807 — verified.
- `GATEWAY_K8S_URL` declaration at kubernetes_spawner.py:124 — verified.
- `_PROTECTED_ENV_KEYS` at kubernetes_spawner.py:138 — verified.
- `setup_anthropic_api` in sandbox/entrypoint.py:712 sets `ANTHROPIC_BASE_URL` at :738 — verified (draft says 737-738, close).
- `allowed_domains.txt` Anthropic-excluded comment block — verified at lines 9-15, says explicitly "api.anthropic.com is intentionally NOT in this allowlist".

The depth of citation (function + line + role in the request lifecycle) is well above the bar for a refine artifact and will give the planner a sturdy anchor to write tasks against.

### 3. Options Analysis — well-decomposed
Four options (A: gateway router + session metadata; B: LiteLLM-fronts-everything; C: route on request-body model name; D: egg_agent SDK bypass). They are meaningfully different along the right axes (where the routing decision lives, what changes on the Claude path, how the compaction-math mitigation gets supported, what new harness surface gets introduced). The pro/con bullets for B and C are explicit about which constraint each fails — B fails the "no regression on the Claude path" gate; C fails the compaction-mitigation requirement that Claude Code be shown a recognized alias even when the backend is Qwen. The reasoning is auditable.

### 4. Constraints and Dependencies — comprehensive
Constraints section (lines 61-71) enumerates: no Claude-path regression, routing-point placement below SSE accumulator + tool-filter + stream resilience, zero-credential sandbox invariant, gateway-mediated visibility, Squid network policy, per-agent independence, model-alias-only (no snapshot pins), file-size discipline against the 1500-line / 100KB cap, and the build-now / validate-later split. The runtime-primitives table doubles as a dependency graph for the planner. Primary risk (Claude Code's compaction math driving auto-compact on unrecognized models, lines 49-59) is well-explained with two external references and a concrete mitigation pointer.

### 5. Open Questions — actionable + properly scaffolded
11 decisions (cq-1 through cq-11) and 5 feedback questions (Q1-Q5) cover, with no obvious gaps I can identify:
- topology (cq-1: deployment vs sidecar vs separate ns)
- routing signal (cq-2: session metadata vs header vs body)
- config shape (cq-3: PipelineConfig field vs repo YAML vs CLI vs stacked precedence)
- validation target (cq-4: which role flips first)
- harness choice (cq-5: Claude Code vs egg_agent SDK)
- backend (cq-6: self-hosted Qwen vs hosted Qwen vs OpenAI smoke test)
- credentials (cq-7: gateway-held vs gateway-passthrough vs sandbox-held)
- failure policy (cq-8: fail-closed vs Claude-fallback vs HITL-on-failure)
- private-mode tool-strip (cq-9: keep vs upstream-aware vs document-and-defer)
- slice decomposition (cq-10: single PR vs parallel vs dependent)
- `[1m]` syntax handling (cq-11: leave vs refactor vs deprecate)

Plus 5 feedback questions covering Qwen hardware/budget, target role list, swap-out interface for LiteLLM, cost tracking extension, and compliance/data-residency. Verified contract state via mcp__sdlc__show_contract: all 11 decisions present with their full option lists, feedback Q1-Q5 present and unresolved. The "Resolved in Pre-Refine" section is correctly empty (the issue has no `## Additional Context` block, which the draft accurately notes at line 177). No silent assumptions detected.

### 6. Recommendation Quality — clear and justified
Option A is recommended with four specific justifications (lines 135-141) that map back to the constraints. The recommendation is conditional on the operator answering cq-1 through cq-11, which is the correct posture for refine: the architectural shape is recommended, the topology / config-shape / first-target details are surfaced for human resolution rather than presumed.

### 7. HITL Decision Registration — properly scaffolded
Cross-checked the `<!-- egg-decision id=cq-N -->` markers in the draft (lines 181, 190, 199, 209, 219, 228, 237, 246, 255, 264, 274) against the contract JSON: all 11 decisions exist with `phase: "refine"`, `type: "hitl"`, `resolved: false`, and the option labels in the contract match the bullet labels in the draft. The `<!-- egg-feedback id=feedback-1 -->` marker (line 285) produced the feedback bundle with Q1-Q5 — verified all five questions are present in `contract.feedback.questions` with `answer: null` and `submitted: false`. The draft is not proceeding on any unvalidated silent assumption.

## Non-blocking notes

- **gateway/gateway.py line numbers off-by-1** — `proxy_anthropic_messages` is cited as `gateway/gateway.py:9752` (lines 32, 152) but actually starts at 9753 (the `@app.route` decorator is on 9752); `proxy_count_tokens` cited at :10019 is actually at :10020; `get_session_by_ip` call cited at :9774 is at :9775. The planner will not be misled (the surrounding context and symbol names are correct), but worth a one-line fix-up if there's a re-propose for another reason. Not blocking on its own.
- **kubernetes_spawner.py:1582 in the Current-Behavior table (line 38)** — cited as a usage site for `overseer_decision_maker_model` (Tier-2), but line 1582 is actually inside the docstring (`decision_model: LLM model for overseer decisions.`); the real usage sites are :1596 (env var) and :1623 (constructor kwarg). The Runtime-primitives table (line 162) cites :1596 correctly, so this is just a minor inconsistency between the two tables.
- **Supply-chain mitigation seam (Q3-adjacent)** — The draft's supply-chain caveat at line 13 and feedback Q3 frame this well, but consider whether the recommended Option A description (line 77) should explicitly call out that `UpstreamRegistry` is the natural seam where a swap-out interface lives. The plan can derive this from Q3, but a one-liner in the recommendation would harden the link.
- **`[1m]` triple-bake risk (cq-11)** — Worth noting non-blockingly that even on option-A "leave it", any code path that builds an agent invocation and forgets to strip `[1m]` for non-Claude routes is a latent foot-gun. cq-11 is the right place to surface this; just flagging that the answer may have implications for how the plan structures the consensus-wrapper signature change.
- **Refine cycle counter** — `contract.refine_review_cycles == 0`, consistent with this being cycle 1.

Overall verdict: this is a high-quality refine artifact. Citations are accurate to the line, constraints are exhaustive, options are meaningfully different and clearly trade-off-justified, the recommended approach maps back to the hard constraints, and all open questions are properly scaffolded into the contract for the operator. The few off-by-1s and the docstring-vs-usage cite for kubernetes_spawner.py:1582 are cosmetic. Ready to advance.


````yaml
id: 07e2ef20-518d-4c
phase: refine
metadata:
  payload:
    artifact_references:
    - .egg-state/drafts/2769-analysis.md
    - .egg-state/contracts/issue-2769.json
    - gateway/gateway.py
    - orchestrator/models.py
    - orchestrator/consensus_wrapper.py
    - orchestrator/concurrent_executor.py
    - orchestrator/routes/pipelines.py
    - orchestrator/kubernetes_spawner.py
    - shared/egg_agent/client.py
    - shared/egg_agent/__main__.py
    - sandbox/llm/runner.py
    - sandbox/entrypoint.py
    - gateway/allowed_domains.txt
    - k8s/base/gateway-deployment.yaml
    reason: "\nReviewed the refine analysis at `.egg-state/drafts/2769-analysis.md`\
      \ (commit 9dc90338c) against all seven review-criteria sections, plus cross-checked\
      \ every cited file/line against the working tree, plus verified the contract\
      \ scaffolding produced the expected 11 decisions + 5 feedback questions.\n\n\
      ## Section-by-Section Evaluation\n\n### 1. Problem Understanding \u2014 strong\n\
      The Problem Statement (lines 5-13) correctly extracts all three hard requirements\
      \ from the issue body (parallel Claude + non-Claude, per-agent independent selectability,\
      \ no-regression on the Claude path) and names the architectural intent (gateway-as-router,\
      \ LiteLLM as translation layer). The side-benefit (harness decoupling) and the\
      \ rejection rationale for `claude-code-router` are preserved. The supply-chain\
      \ footnote on the March 2026 LiteLLM PyPI incident (line 13) is a useful unprompted\
      \ add \u2014 it surfaces a real risk without overweighting it.\n\n### 2. Research\
      \ Quality \u2014 exemplary\nSpot-checked the 22 entries in the Runtime-primitives\
      \ table against the working tree:\n- `get_anthropic_client` cited at gateway.py:9316-9329\
      \ \u2014 actual definition spans 9317 (singleton decl) to 9329 (return), match.\n\
      - `_inject_anthropic_credentials` at :9355 \u2014 verified.\n- `_filter_blocked_tools`\
      \ at :9410 \u2014 verified.\n- `_SSEAccumulator` at :9552 \u2014 verified.\n\
      - `get_session_by_ip` lookup in `proxy_anthropic_messages` \u2014 verified at\
      \ gateway.py:9775 (draft says 9774, off-by-1).\n- `proxy_anthropic_messages`\
      \ \u2014 actual `def` is at gateway.py:9753 (draft says 9752, off-by-1 \u2014\
      \ the `@app.route` decorator is on the prior line).\n- `proxy_count_tokens`\
      \ \u2014 actual at gateway.py:10020 (draft says 10019, same off-by-1).\n- `overseer_decision_maker_model`\
      \ / `overseer_advisor_model` Field decls at orchestrator/models.py:546 and :620\
      \ \u2014 verified.\n- `build_consensus_wrapped_command(model=\"opus\", ...)`\
      \ at orchestrator/consensus_wrapper.py:620-622 with `--model` at :658 \u2014\
      \ verified, falls inside the cited 653-662 range.\n- Call sites at concurrent_executor.py:454\
      \ and routes/pipelines.py:2704 with no model arg \u2014 verified, confirming\
      \ the \"every non-overseer agent is opus-only by hardcoding\" claim.\n- `DEFAULT_MODEL\
      \ = \"opus[1m]\"` at shared/egg_agent/client.py:62 \u2014 verified.\n- `parser.add_argument(\"\
      --model\", default=\"opus[1m]\", ...)` at shared/egg_agent/__main__.py:35 \u2014\
      \ verified.\n- `cmd.extend([\"--model\", \"opus[1m]\"])` at sandbox/llm/runner.py:49\
      \ \u2014 verified.\n- `ANTHROPIC_BASE_URL=GATEWAY_K8S_URL` at orchestrator/kubernetes_spawner.py:807\
      \ \u2014 verified.\n- `GATEWAY_K8S_URL` declaration at kubernetes_spawner.py:124\
      \ \u2014 verified.\n- `_PROTECTED_ENV_KEYS` at kubernetes_spawner.py:138 \u2014\
      \ verified.\n- `setup_anthropic_api` in sandbox/entrypoint.py:712 sets `ANTHROPIC_BASE_URL`\
      \ at :738 \u2014 verified (draft says 737-738, close).\n- `allowed_domains.txt`\
      \ Anthropic-excluded comment block \u2014 verified at lines 9-15, says explicitly\
      \ \"api.anthropic.com is intentionally NOT in this allowlist\".\n\nThe depth\
      \ of citation (function + line + role in the request lifecycle) is well above\
      \ the bar for a refine artifact and will give the planner a sturdy anchor to\
      \ write tasks against.\n\n### 3. Options Analysis \u2014 well-decomposed\nFour\
      \ options (A: gateway router + session metadata; B: LiteLLM-fronts-everything;\
      \ C: route on request-body model name; D: egg_agent SDK bypass). They are meaningfully\
      \ different along the right axes (where the routing decision lives, what changes\
      \ on the Claude path, how the compaction-math mitigation gets supported, what\
      \ new harness surface gets introduced). The pro/con bullets for B and C are\
      \ explicit about which constraint each fails \u2014 B fails the \"no regression\
      \ on the Claude path\" gate; C fails the compaction-mitigation requirement that\
      \ Claude Code be shown a recognized alias even when the backend is Qwen. The\
      \ reasoning is auditable.\n\n### 4. Constraints and Dependencies \u2014 comprehensive\n\
      Constraints section (lines 61-71) enumerates: no Claude-path regression, routing-point\
      \ placement below SSE accumulator + tool-filter + stream resilience, zero-credential\
      \ sandbox invariant, gateway-mediated visibility, Squid network policy, per-agent\
      \ independence, model-alias-only (no snapshot pins), file-size discipline against\
      \ the 1500-line / 100KB cap, and the build-now / validate-later split. The runtime-primitives\
      \ table doubles as a dependency graph for the planner. Primary risk (Claude\
      \ Code's compaction math driving auto-compact on unrecognized models, lines\
      \ 49-59) is well-explained with two external references and a concrete mitigation\
      \ pointer.\n\n### 5. Open Questions \u2014 actionable + properly scaffolded\n\
      11 decisions (cq-1 through cq-11) and 5 feedback questions (Q1-Q5) cover, with\
      \ no obvious gaps I can identify:\n- topology (cq-1: deployment vs sidecar vs\
      \ separate ns)\n- routing signal (cq-2: session metadata vs header vs body)\n\
      - config shape (cq-3: PipelineConfig field vs repo YAML vs CLI vs stacked precedence)\n\
      - validation target (cq-4: which role flips first)\n- harness choice (cq-5:\
      \ Claude Code vs egg_agent SDK)\n- backend (cq-6: self-hosted Qwen vs hosted\
      \ Qwen vs OpenAI smoke test)\n- credentials (cq-7: gateway-held vs gateway-passthrough\
      \ vs sandbox-held)\n- failure policy (cq-8: fail-closed vs Claude-fallback vs\
      \ HITL-on-failure)\n- private-mode tool-strip (cq-9: keep vs upstream-aware\
      \ vs document-and-defer)\n- slice decomposition (cq-10: single PR vs parallel\
      \ vs dependent)\n- `[1m]` syntax handling (cq-11: leave vs refactor vs deprecate)\n\
      \nPlus 5 feedback questions covering Qwen hardware/budget, target role list,\
      \ swap-out interface for LiteLLM, cost tracking extension, and compliance/data-residency.\
      \ Verified contract state via mcp__sdlc__show_contract: all 11 decisions present\
      \ with their full option lists, feedback Q1-Q5 present and unresolved. The \"\
      Resolved in Pre-Refine\" section is correctly empty (the issue has no `## Additional\
      \ Context` block, which the draft accurately notes at line 177). No silent assumptions\
      \ detected.\n\n### 6. Recommendation Quality \u2014 clear and justified\nOption\
      \ A is recommended with four specific justifications (lines 135-141) that map\
      \ back to the constraints. The recommendation is conditional on the operator\
      \ answering cq-1 through cq-11, which is the correct posture for refine: the\
      \ architectural shape is recommended, the topology / config-shape / first-target\
      \ details are surfaced for human resolution rather than presumed.\n\n### 7.\
      \ HITL Decision Registration \u2014 properly scaffolded\nCross-checked the `<!--\
      \ egg-decision id=cq-N -->` markers in the draft (lines 181, 190, 199, 209,\
      \ 219, 228, 237, 246, 255, 264, 274) against the contract JSON: all 11 decisions\
      \ exist with `phase: \"refine\"`, `type: \"hitl\"`, `resolved: false`, and the\
      \ option labels in the contract match the bullet labels in the draft. The `<!--\
      \ egg-feedback id=feedback-1 -->` marker (line 285) produced the feedback bundle\
      \ with Q1-Q5 \u2014 verified all five questions are present in `contract.feedback.questions`\
      \ with `answer: null` and `submitted: false`. The draft is not proceeding on\
      \ any unvalidated silent assumption.\n\n## Non-blocking notes\n\n- **gateway/gateway.py\
      \ line numbers off-by-1** \u2014 `proxy_anthropic_messages` is cited as `gateway/gateway.py:9752`\
      \ (lines 32, 152) but actually starts at 9753 (the `@app.route` decorator is\
      \ on 9752); `proxy_count_tokens` cited at :10019 is actually at :10020; `get_session_by_ip`\
      \ call cited at :9774 is at :9775. The planner will not be misled (the surrounding\
      \ context and symbol names are correct), but worth a one-line fix-up if there's\
      \ a re-propose for another reason. Not blocking on its own.\n- **kubernetes_spawner.py:1582\
      \ in the Current-Behavior table (line 38)** \u2014 cited as a usage site for\
      \ `overseer_decision_maker_model` (Tier-2), but line 1582 is actually inside\
      \ the docstring (`decision_model: LLM model for overseer decisions.`); the real\
      \ usage sites are :1596 (env var) and :1623 (constructor kwarg). The Runtime-primitives\
      \ table (line 162) cites :1596 correctly, so this is just a minor inconsistency\
      \ between the two tables.\n- **Supply-chain mitigation seam (Q3-adjacent)**\
      \ \u2014 The draft's supply-chain caveat at line 13 and feedback Q3 frame this\
      \ well, but consider whether the recommended Option A description (line 77)\
      \ should explicitly call out that `UpstreamRegistry` is the natural seam where\
      \ a swap-out interface lives. The plan can derive this from Q3, but a one-liner\
      \ in the recommendation would harden the link.\n- **`[1m]` triple-bake risk\
      \ (cq-11)** \u2014 Worth noting non-blockingly that even on option-A \"leave\
      \ it\", any code path that builds an agent invocation and forgets to strip `[1m]`\
      \ for non-Claude routes is a latent foot-gun. cq-11 is the right place to surface\
      \ this; just flagging that the answer may have implications for how the plan\
      \ structures the consensus-wrapper signature change.\n- **Refine cycle counter**\
      \ \u2014 `contract.refine_review_cycles == 0`, consistent with this being cycle\
      \ 1.\n\nOverall verdict: this is a high-quality refine artifact. Citations are\
      \ accurate to the line, constraints are exhaustive, options are meaningfully\
      \ different and clearly trade-off-justified, the recommended approach maps back\
      \ to the hard constraints, and all open questions are properly scaffolded into\
      \ the contract for the operator. The few off-by-1s and the docstring-vs-usage\
      \ cite for kubernetes_spawner.py:1582 are cosmetic. Ready to advance.\n"
    ack_version: 1
  version: 1
````

### [2026-05-22T01:33:32Z] orchestrator → refiner (STATUS): Ready to confirm — all confirm preconditions satisfied

Your proposal (version 1) is ready to confirm — all blocking reviews are clear and global confirm preconditions are met. Run `egg-orch consensus confirmed` to confirm.

````yaml
id: dee84602-f761-45
phase: refine
metadata:
  ready_to_confirm: true
  version: 1
````

### [2026-05-22T01:33:32Z] refiner (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: 10b1fc6c-c5cb-42
phase: refine
metadata:
  state: WORKING
````

### [2026-05-22T01:33:35Z] refiner (CONSENSUS_CONFIRMED): Confirmed by refiner

````yaml
id: af619638-534d-45
phase: refine
metadata:
  consensus_reached: false
````

### [2026-05-22T01:33:35Z] reviewer_agent_design (HEARTBEAT): heartbeat: WORKING

wait_loop exited

````yaml
id: c06b8ad9-bf0a-47
phase: refine
metadata:
  state: WORKING
````

### [2026-05-22T01:33:38Z] reviewer_refine (CONSENSUS_CONFIRMED): Confirmed by reviewer_refine

````yaml
id: 05db1873-72ea-4f
phase: refine
metadata:
  consensus_reached: true
````

### [2026-05-22T01:33:40Z] reviewer_agent_design (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_PROPOSE,CONSENSUS_RE_REVIEW,CONSENSUS_CONFIRMED,OVERSEER_ALERT

````yaml
id: d7d1a265-7da8-4a
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:33:40.083389+00:00'
````

### [2026-05-22T01:33:41Z] refiner (HEARTBEAT): heartbeat: WAITING_FOR_EVENT

wait_loop blocked on CONSENSUS_CONFIRMED,CONSENSUS_RE_REVIEW,OVERSEER_ALERT

````yaml
id: a9e6805b-0c48-4b
phase: refine
metadata:
  state: WAITING_FOR_EVENT
  since: '2026-05-22T01:33:41.691260+00:00'
````
