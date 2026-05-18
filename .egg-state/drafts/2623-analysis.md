# Analysis: Run the full egg SDLC stack natively in Claude Code (substrate swap)

> Issue: #2623 | Phase: refine

## Problem Statement

The egg SDLC stack today runs on a **k3s + Redis Streams + Docker + sidecar gateway** substrate (`orchestrator/kubernetes_spawner.py`, `orchestrator/redis_message_store.py`, `gateway/gateway.py`, `sandbox/Dockerfile`). Running egg requires standing up a k3s cluster, building/loading sandbox images, running Redis, and proxying Anthropic credentials through a long-lived gateway. That is a heavy install footprint for a tool whose value proposition — agent-driven SDLC pipelines — is increasingly something a single developer wants to run from a vanilla Claude Code session.

PR #2608 shipped a **`refine-plan` Markdown skill** (`plugins/refine-plan/skills/refine-plan/SKILL.md`, 451 lines) that *approximates* egg's refine+plan phases inside Claude Code using subagents and a filesystem verdict journal. Child issues #2612 (concurrent BRC) and #2622 (depth gap) catalogue the structural shortcomings of that approximation: it is iterated rather than concurrent, has no real message bus or version tracking, no mid-cycle revision, no `build_system_prompt` depth, and reviewer rubrics that don't enforce evidence breadth.

The North Star this parent issue articulates is a different framing entirely. Rather than chase parity with a parallel Markdown implementation, **swap the substrate underneath the real stack**: keep `egg_orchestrator`, `egg_harness`, `egg_contracts`, `egg_agent`, `shared/prompts/` unchanged, and re-platform them onto Claude Code's native primitives:

| Today's substrate | Claude-Code-native replacement |
|---|---|
| k3s schedules agent pods (`KubernetesSpawner`) | `Agent` tool spawns subagents |
| Docker container per agent (`sandbox/Dockerfile`) | `Agent` tool's `isolation: "worktree"` + Bash sandbox mode |
| Redis Streams message bus (`RedisMessageStore`) | In-process Python (`MessageStore` interface already exists) |
| Gateway sidecar policy + creds (`gateway/`) | PreToolUse hooks in `.claude/settings.json` |
| Persistent volume for worktrees | User's local filesystem |
| Sandbox container image | User's local Claude Code install + `agents/*.md` |
| `kubectl get pods` health checks | In-process `egg_health` thread |
| Overseer pod | In-process overseer thread |

If the swap is real (vs. an approximation), depth, BRC mechanics, contract schema, role prompts, and HITL semantics come for free from the reused upstream code. **Quality becomes structurally inevitable**: the only question is "does the substrate-swap layer faithfully expose the orchestrator's coordination surface to a Claude Code session?"

The desired outcome:

1. A user with Claude Code installed and an Anthropic API key can run **the full egg SDLC** (refine → plan → implement → PR) against an issue from inside their session, with **no k3s, Docker, Redis, or gateway daemon** running.
2. The same `egg_orchestrator` Python that runs in k3s today runs in-process to the parent Claude session, dispatching agents via the `Agent` tool instead of `KubernetesSpawner`.
3. The conformance suite (integration tests already shipped under `integration_tests/regression/` from #2474) passes against **both** substrates — proving behavioral equivalence.
4. The substrate-swap architecture is documented in an ADR-style file in the repo.

## Current Behavior

### Orchestrator boot is a Flask HTTP daemon

`orchestrator/cli.py::cmd_serve()` (lines 83–150) imports `api.py` and calls `waitress.serve()` on port 9849. There is **no in-process / embedded / local mode** — a grep for `embedded`, `local`, `in_process` finds nothing. `egg-orch` (the CLI sandbox agents call) is a thin HTTP client; it POSTs to the orchestrator API. All BRC coordination, message-bus interaction, and agent dispatch flow through this HTTP boundary.

The Flask app is composed of 12 route blueprints under `orchestrator/routes/`; most handler logic is wrapped in `jsonify()` and not currently callable as a library API.

### Message bus has a clean abstraction; the production binding is Redis

`orchestrator/message_store.py` defines a `MessageStore` interface (in-memory dict per pipeline) and `Message`/`MessageType` Pydantic types. `orchestrator/redis_message_store.py` wraps Redis Streams. Routes already access messages through the store interface (`get_messages()`, `add_message()`), so the swap surface is small: instantiate `MessageStore` instead of `RedisMessageStore`, or add an in-process bus variant that's drop-in compatible.

Only three files import the `redis` package directly: `redis_message_store.py`, `routes/anchors.py`, and tests. Most of the orchestrator is bus-implementation-agnostic.

### Agent dispatch is hardcoded to k8s

`orchestrator/kubernetes_spawner.py::KubernetesSpawner.spawn_agent_job()` constructs a k8s Job manifest with labels (`LABEL_PIPELINE_ID`, `LABEL_AGENT_ROLE`), service URLs (`GATEWAY_K8S_URL`, `ORCHESTRATOR_K8S_URL`), worktree volume mounts, and posts it to the k8s API. `orchestrator/kubernetes_monitor.py` polls k8s for pod status changes (~2 KLOC). `orchestrator/container_spawner.py` is a 36-line back-compat shim that aliases `ContainerSpawner = KubernetesSpawner`.

There is no `AgentSpawner` interface today; the orchestrator just imports `KubernetesSpawner` directly in `concurrent_executor.py::_spawn_agent()`. This is the **largest abstraction gap** in the codebase for the substrate swap.

### Gateway is an HTTP sidecar with portable policy logic

`gateway/gateway.py` exposes Flask REST endpoints (`/api/v1/git/push`, `/api/v1/github/...`, `/api/v1/worktrees/create`, `/api/v1/anthropic/...`). The policy logic (allow/deny by phase + role + path) lives in `gateway/agent_restrictions.py` and is a pure Python module. The credential layer (`gateway/anthropic_credentials.py`, `gateway/github_client.py`) holds tokens server-side and injects them into upstream calls so the sandbox never sees them.

For Claude Code substrate, `agent_restrictions.py` is a directly-importable module; the HTTP wrapper isn't load-bearing. The credentials story is different: in Claude Code the user's session already holds the API key, so there's no need to "inject" — but PreToolUse hooks (or the existing MCP tools) would still need to enforce the same allow/deny rules.

### Contracts and checkpoints are already filesystem-native

Contracts live at `<repo>/.egg-state/contracts/<id>.json` as Pydantic-serializable `Contract` v1.1 objects (`shared/egg_contracts/models.py`). BRC history (`.egg-state/brc-history/<id>-<phase>.json`) and agent outputs (`.egg-state/agent-outputs/`) are JSON on disk. Worktrees default to `WORKTREE_BASE_DIR=/home/egg/.egg-worktrees/<pipeline_id>/<repo>` (env-var overridable, no k8s coupling).

This entire data plane is portable. The substrate swap doesn't touch storage format.

### Existing refine-plan skill is a useful template, not the destination

`plugins/refine-plan/skills/refine-plan/SKILL.md` shows the shape of a Claude-Code-orchestrated SDLC: parent session reads role markdown, prepends per-role rubric, spawns subagents via `Agent` tool with `subagent_type: "general-purpose"`, journals verdicts to `.refine-plan-state/<id>/`. It does **not** import `egg_orchestrator` — it's a parallel implementation. #2622 documents the resulting depth gap (narrow research, narrative-not-evidence trade-offs, no live mid-cycle revision).

The substrate-swap target moves the other direction: keep the orchestrator unchanged, push the Claude-Code primitives *underneath* it.

### Harness already supports Claude Code

`shared/egg_harness/` is the real agent runtime (replaces Claude Agent SDK as egg's primary harness). It already supports multiple providers, multi-KB role system prompts via `build_system_prompt(sources)`, JSONL session persistence, permission-callback gating. `EGG_HARNESS=claude-code` selects a Claude Code harness binding. So a "in-process orchestrator" doesn't need to invent agent runtime — it just needs to invent **agent spawning** (and let the harness handle the rest, OR replace harness-driven dispatch with `Agent`-tool-driven dispatch).

## Constraints

### Hard constraints from the issue body
- **`SendMessage` is platform-gated** (anthropics/claude-code#36196). Mid-cycle peer-to-peer agent messaging — what would let a producer revise mid-cycle after a reviewer NACK — is not currently available. The orchestrator-as-message-bus model (what egg does today via Redis Streams) sidesteps this for **BRC mechanics correctness**; it remains a *quality booster*, not a *correctness requirement*.
- **Subagent context windows vs. `max_turns: 1000`** (`docs/guides/concurrent-execution.md:97`). A subagent in Claude Code has a smaller context than an egg sandbox. Deep refines that span 30+ files may exhaust the window. Egg's `egg_container` checkpoint primitives are the existing answer; whether they port cleanly is open.
- **HITL must surface through the parent session**. The k3s pipeline lets HITL flow through MCP `provide_input` while the orchestrator stays alive in a long-running pod. In Claude Code, the orchestrator is in-process to the parent session; pausing means returning control to the parent so it can `AskUserQuestion` (or surface the decision via comment), then resuming.
- **Concurrency ceiling on `Agent` tool spawns**. Egg's `ThreadPoolExecutor` in `concurrent_executor.py` spawns N agents per phase; Claude Code has a practical ceiling on parallel subagents. Should map cleanly for ≤6-role phases but may bite the larger slice-DAG phases.
- **Install footprint is larger** than a pure-Markdown skill. The skill now depends on (or ships) the egg Python packages. Per the issue, this is acceptable — but must be documented and the failure mode (missing deps) handled.

### Architectural / design constraints
- **`build_system_prompt(sources)` depth** (`shared/egg_harness/prompt.py`) must reach the subagents — otherwise the depth gap (#2622) reopens. The substrate swap must keep the real prompt assembly in the path, not regress to thin per-role markdown.
- **BRC concurrency invariants** (INV-1..5, `orchestrator/action_guards.py::validate_invariants()`): version tracking on producer re-proposals, stale-ACK un-confirmation, open-NACK barrier. These live in `egg_orchestrator` and survive unchanged if the orchestrator-as-bus model is preserved.
- **Gateway-equivalent enforcement**. The integration-test trust-boundary doc (`docs/architecture/integration-test-trust-boundary.md`) distinguishes in-sandbox-agent / trusted-CI-runner / human-operator execution contexts. The substrate swap shifts most agent execution from "in-sandbox" to "in-parent-Claude-Code-session" — that's a new trust context. PreToolUse hooks must be the structural enforcement layer; prompt-only restrictions are insufficient.
- **File-write boundaries (`shared/egg_restrictions/patterns.py`)** must continue to enforce role-based path restrictions. The gateway enforces these on `git push` today; the Claude Code substrate needs an equivalent — either via PreToolUse hooks intercepting writes, or via the MCP `check_file_restriction` tool agents already query.

### Conformance / proof obligation
- The issue's definition of done requires **passing the conformance suite on ≥5 representative issues across both substrates**. The integration tests at `integration_tests/regression/` (BRC happy-path, live-pod guard, unpushed-commit salvage, HITL round-trip, slice-DAG restart, phase-aware timeouts, babysit-PR single-final-push) are k3s-shaped. Some — like "live-pod guard on restart" — are k3s-specific concerns that don't translate to a single-process substrate. The conformance set needs **substrate-portable invariants**, not k3s-specific assertions, factored out of the existing suite.

### Dependencies on other systems / features
- Marketplace packaging (`.claude-plugin/plugin.json` exists for refine-plan, 22 lines). Distribution path for a heavier skill (with Python deps) is open.
- `docs/guides/harness-configuration.md` already documents three harness modes (`claude-sdk`, `claude-code`, `egg`). The substrate swap should pick one and document the choice, or thread the existing selector through.

## Options Considered

### Option A: Abstraction-first, parallel substrates, feature-flagged

**Approach**: Land `AgentSpawner`, `MessageBus`, `PolicyEnforcer`, `WorktreeManager` interfaces in `egg_orchestrator`. Keep `KubernetesSpawner` + `RedisMessageStore` + `gateway/` as one implementation. Add `ClaudeCodeSpawner` + `InProcessMessageBus` + `PreToolUseHookPolicy` as a second implementation. Select via env var (e.g. `EGG_SUBSTRATE=claude-code` vs `EGG_SUBSTRATE=k3s`). Both substrates run the same integration suite as a CI matrix; conformance is structurally verified rather than asserted. The skill is a thin entry point that boots the in-process orchestrator with the claude-code substrate selected.

**Pros**:
- The conformance proof is **executable** — both substrates run the same test, divergence is immediately visible.
- Migration risk is contained: k3s users keep working; Claude Code users get a new path.
- The abstraction layer is the document — code shapes naturally explain the substrate boundary, reducing ADR-vs-reality drift.
- Refactor is interface-driven, naturally chunkable into per-component slices (spawner, bus, gateway, worktree).
- The four "structural causes" #2622 catalogs (`general-purpose` subagents, missing depth targets, no mid-cycle revision, no tool-use budget signaling) get fixed for both substrates simultaneously when the real `build_system_prompt` flows through.

**Cons**:
- Doubles the maintained surface area until k3s is deprecated.
- The interfaces have to be designed to fit Claude Code constraints (subagent ceiling, no `SendMessage`) without unduly constraining the k3s implementation.
- Conformance test factoring takes real work — the existing tests assume k3s/Redis primitives.

### Option B: All-at-once full substrate replacement (delete k3s)

**Approach**: Replace `KubernetesSpawner` with `ClaudeCodeSpawner` directly. Delete `kubernetes_monitor.py`, the gateway sidecar, Redis Streams. Re-implement integration tests for the new substrate only.

**Pros**:
- One substrate to maintain.
- Code is simpler post-cut: no abstraction layer, no env-var dispatch.
- The Claude Code substrate becomes the default and only substrate; "egg" effectively becomes a Claude Code skill.

**Cons**:
- **High blast radius**. k3s users (CI, anyone running egg today) are broken until the swap is complete and conformance is passing.
- The conformance proof becomes "Claude Code substrate ships green" — which is necessary but not sufficient. The hard question (does the substrate swap faithfully reproduce the orchestrator's coordination behavior?) is harder to answer without a side-by-side comparison.
- Loses the structural ability to ever run the orchestrator outside Claude Code (e.g. for CI integration tests, batch automation against a registry of issues, server-mode deployments).

### Option C: Skill-only — don't touch the orchestrator

**Approach**: Treat the issue body's framing as aspirational; ship a beefier `refine-plan` (or `sdlc`) skill that imports `egg_orchestrator` Python *but only for non-protocol code* (contract schema, prompt assembly, file restrictions). Continue using verdict-journal-style BRC. Close #2622 by routing through `build_system_prompt`. Defer concurrent BRC to #2612.

**Pros**:
- Smallest code change; no orchestrator refactor required.
- Skill stays portable (Python deps but no Redis/k3s).
- Depth gap closes structurally because `build_system_prompt` is in the path.

**Cons**:
- **Misses the issue's North Star**: this is parity-chasing, not substrate-swap. BRC mechanics (version tracking, open-NACK barrier, mid-cycle revision) remain "BRC-inspired" approximations, exactly the gap #2612 already documents.
- The conformance suite cannot be reused — there's no real orchestrator to test.
- Effectively closes #2622 only, leaving #2612 and the parent's substrate-swap intent unresolved.

### Option D: In-process orchestrator binding, single substrate, no abstraction layer

**Approach**: Build a `ClaudeCodeSpawner` and `InProcessMessageBus` that the existing orchestrator code can use *when imported into a Claude Code session*. Don't extract clean interfaces — instead, monkey-patch / dependency-inject the new components at the boot path. The orchestrator file structure stays, but module-level imports become per-mode.

**Pros**:
- Less refactoring than Option A.
- The orchestrator code base doesn't grow new abstraction layers.
- Faster to a working demo.

**Cons**:
- The substrate boundary isn't visible in the code — it's implicit in the import order and DI wiring. ADR-vs-reality drift is high.
- Conformance proof depends on inspection rather than execution.
- Testing both substrates from the same suite is painful (mocking-heavy).
- Long-term maintenance hazard: the "two implementations" exist as untyped facts about boot-time configuration, not as named interfaces.

## Recommended Approach

**Option A — abstraction-first parallel substrates with conformance-by-CI-matrix.**

Justification:

1. **The conformance proof is executable, not narrative.** The issue's definition of done requires "passes the same behavioral conformance tests as the k3s substrate, on a measurement set of ≥5 representative issues." Option A makes this a CI matrix dimension rather than a manual test plan. Option B forfeits the comparison; Option D's coupling makes the comparison expensive to maintain.

2. **The Claude Code constraints (`SendMessage` gating, subagent ceiling, context windows) are best surfaced as interface contracts.** `AgentSpawner.spawn(...)` and `MessageBus.send(...)` named on an interface let the constraints live as method signatures, return types, and documented limitations. Option D's monkey-patching hides them; Option C never confronts them.

3. **Depth gap closure is structural in Option A.** Once `build_system_prompt(sources)` is in the Claude Code path, the four #2622 causes (general-purpose subagents, missing depth targets, no mid-cycle revision via bus, no tool-use budget) close as the natural consequence of running the real harness inside a Claude Code session — not as a parallel rubric rewrite of the skill.

4. **It respects the issue body's exclusion of #2474, #2612, #2622 from this issue's must-do list** — the children are absorbed structurally (the conformance suite from #2474 stays load-bearing; the depth gap and concurrent BRC close because the real orchestrator is in the path), but this parent doesn't need to fully solve them, only verify the substrate carries them.

5. **Migration safety is preserved.** k3s deployments (CI, server-mode automation) keep working until the operator decides to deprecate; the Claude Code substrate is additive.

The work tracked by this parent is therefore **the abstraction layer + the two implementations + the conformance matrix + the ADR**. The plan phase will decide the slice-DAG shape for the rollout; this analysis stays focused on framing.

## Open Questions

Every open question below is registered as a contract decision or feedback item. Decision IDs map to the order they were created.

### Pre-Refine Context (already settled by the operator's framing update)

The issue body itself replaced the original "S1/S2/S3 substrate options" framing with the substrate-swap framing, and the operator's comment on the issue explicitly cleared three sub-decisions:

- **Substrate intent**: pursue substrate-swap, not parity-chase (rules out Option C below in spirit).
- **#2474 disposition**: integration tests stay as the cross-substrate conformance suite.
- **#2612 disposition**: largely absorbed; `SendMessage`/Agent Teams remains a quality booster, not a correctness requirement (orchestrator-as-bus is the answer for BRC mechanics).
- **#2622 disposition**: largely absorbed; real `build_system_prompt` closes the depth gap structurally.

These are noted here so the plan phase doesn't re-litigate them. Everything else is open.

### Registered decisions and feedback

The following questions were registered via `egg-contract` and will be populated with their markdown checkboxes after this section.

<!-- egg-hitl-decision id=cq-1 -->

**Substrate coexistence strategy: how should the Claude Code substrate relate to the existing k3s substrate?**

- [ ] Parallel substrates, env-var-selected: AgentSpawner/MessageBus/PolicyEnforcer interfaces in egg_orchestrator with two implementations; EGG_SUBSTRATE=claude-code|k3s; both run the same conformance suite (Option A)
- [ ] Full cut-over to Claude Code substrate: delete k3s/Redis/Docker/gateway code; orchestrator only runs in-process to a Claude Code session (Option B)
- [ ] Skill-only: do not refactor the orchestrator; ship a beefier refine-plan/sdlc skill that imports egg_orchestrator Python for non-protocol code (Option C — contradicts the issue's substrate-swap framing, listed for completeness)
- [ ] In-process binding without interfaces: monkey-patch / DI the new spawner+bus at boot path; no named abstraction layer (Option D)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-2 -->

**Initial pipeline-phase scope for the substrate swap: which phases must work on the Claude Code substrate before this parent issue closes?**

- [ ] Refine + plan only: prove the substrate on the existing refine-plan skill's footprint; defer implement+pr to a follow-up
- [ ] All phases (refine + plan + implement + pr): full SDLC inside Claude Code; matches the issue body's North Star verbatim
- [ ] Explore-first: ship the abstraction interfaces + a single-role 'spawn one agent against an issue' smoke path; defer multi-role + BRC to follow-up issues once the spawner shape is settled
- [ ] Refine + plan + implement (no pr): prove BRC mechanics on producer roles; let pr-phase keep using k3s for now since PR creation is an external-API operation, not a coordination test
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-3 -->

**Conformance-suite scoping: where does the substrate-portable behavioral test set live?**

- [ ] Extend integration_tests/regression/ with a substrate parameter (CI matrix): each existing test runs under both k3s and claude-code substrates; tests that are inherently k3s-specific (e.g. live-pod-guard) skip on the claude-code dimension
- [ ] New shared conformance package (e.g. integration_tests/conformance/): factor substrate-portable invariants out of integration_tests/regression/; the regression suite keeps the k3s-specific stuff and the new package is what both substrates run
- [ ] Per-substrate test suites: integration_tests/regression/ stays k3s; integration_tests/claude_code/ is the new substrate's suite; cross-substrate conformance becomes a documented set of invariants verified by inspection, not by a single CI matrix
- [ ] Skill-internal cycle harness: the Claude Code substrate gets its own end-to-end smoke test that re-runs the refine-plan skill's existing local repro against ≥5 representative issues; no cross-substrate matrix
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-4 -->

**Agent-spawner interface shape: what method signature should AgentSpawner expose so both KubernetesSpawner and ClaudeCodeSpawner can satisfy it?**

- [ ] Synchronous spawn(role, prompt, env, worktree) -> AgentResult: caller blocks until agent completes; spawner handles internal concurrency. Maps cleanly to the Agent tool's call-and-wait model; k8s implementation polls internally
- [ ] Async dispatch + poll: spawn(...) -> AgentHandle; poll(handle) -> AgentStatus; matches today's KubernetesSpawner+monitor split, but requires the Claude Code spawner to fake handles (no underlying job ID) and complicates the in-process model
- [ ] Stream-shaped: spawn(...) yields events (start, tool-call, output, completion); the orchestrator drives the agent via the event stream; mirrors how egg_harness exposes the agent loop. Most powerful, biggest refactor
- [ ] Agent-tool-direct (Claude Code only): the abstraction is 'request a subagent run' and the k8s implementation translates by spawning a process running egg_harness — the interface is shaped around Claude Code's call shape, k8s adapts
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-5 -->

**Worktree-management ownership in the Claude Code substrate: who creates and tears down the per-agent worktree?**

- [ ] Claude Code's EnterWorktree / ExitWorktree tools: the substrate uses the harness-provided worktree primitive; egg's WORKTREE_BASE_DIR layout is replaced by ~/.claude/worktrees/<pipeline_id>. Native, but loses egg's per-repo-per-pipeline shared-checkout optimization
- [ ] Port egg's WORKTREE_BASE_DIR model: agents run inside .egg-state/<pipeline_id>/ subdirectories on the user's filesystem; the spawner manages worktree creation/teardown explicitly. Preserves egg's checkout-sharing; doesn't use Claude Code's native worktree mechanism
- [ ] Hybrid: parent session creates the pipeline-level shared checkout under .egg-state/<pipeline_id>/; each subagent call passes through EnterWorktree to get an isolated branch; teardown is filesystem-level by the orchestrator at phase end
- [ ] Single-worktree mode: no per-agent worktree; agents run sequentially against the parent session's repo and the BRC bus serializes their writes. Loses concurrency but maximally simple
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-6 -->

**Policy enforcement seam: where does the substrate enforce the gateway-equivalent rules (file-write restrictions per role, git-push allow/deny, gh-operation phase gates)?**

- [ ] PreToolUse hooks in .claude/settings.json: skill-installed hook intercepts Write/Edit/Bash/etc. and calls into shared/egg_restrictions/patterns.py; structural enforcement at the tool boundary
- [ ] MCP-tool-side validators: every state-mutating MCP verb re-validates the caller's role+path against patterns.py; tools the harness uses (Write/Bash) are guarded by the harness's existing permission_callback. No hooks; existing layers strengthened
- [ ] In-process Python imports only: the orchestrator drives subagent prompts that *announce* role restrictions and the subagent self-polices; depends on prompt discipline (not structural enforcement). Listed for completeness — rejected upfront unless cost of structural enforcement is too high
- [ ] All three layered: PreToolUse hook for hard cuts (writes to blocked paths), MCP validators for state-mutation gates, prompt-time restrictions for graceful messaging. Most defense-in-depth, most build cost
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-7 -->

**HITL surface in Claude Code mode: how does the orchestrator pause for a human decision when running in-process to a Claude Code session?**

- [ ] Parent-session AskUserQuestion: orchestrator yields a HITL decision back to the parent Claude session, which surfaces it via AskUserQuestion; user reply resumes the pipeline. Best UX for solo developer; requires the orchestrator's run loop to be reentrant from the parent's perspective
- [ ] MCP provide_input verb: matches today's pattern; the parent calls a provide_input MCP tool from the user's prompt. Awkward inside a single Claude session (user typed something, agent calls back through MCP into the same process)
- [ ] Filesystem journal + parent poll: orchestrator writes the pending decision to .egg-state/hitl/<id>.json; parent polls and surfaces it; user replies via a slash-command or skill verb that writes back. Works without orchestrator-as-callee but adds round-trip friction
- [ ] Heredoc-style synchronous: the orchestrator surface is a generator that yields HITLDecision objects; the skill's outer loop renders them with AskUserQuestion and feeds answers back. Hybrid of 1 and 3
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-8 -->

**Install / packaging footprint: how does the user obtain the egg Python packages required by the in-process orchestrator?**

- [ ] Skill ships Python wheels in the plugin: marketplace install drops wheels into a venv the skill bootstraps; isolates the install from system Python; biggest plugin size
- [ ] Plugin metadata declares pip dependencies: user runs 'pip install egg' (or equivalent) once; plugin.json documents the requirement; smallest plugin but pre-flight check needed before the skill works
- [ ] Vendor minimal subset into the plugin: the skill copies only the egg_orchestrator + egg_contracts + egg_harness + shared/prompts/ subtree it needs; no wheels, no pip; biggest source-of-truth-drift risk between the vendored copy and the upstream
- [ ] Two-skill layout: a thin entry-point skill + a heavier 'egg-runtime' plugin that ships the wheels; the entry-point depends on the runtime; user installs both. More moving parts but cleaner separation
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-9 -->

**k3s deprecation timing: what is the disposition of the k3s substrate after the Claude Code substrate ships green?**

- [ ] Deprecate k3s on substrate-swap merge: remove kubernetes_spawner.py / kubernetes_monitor.py / RedisMessageStore / gateway sidecar in the same PR family; Claude Code substrate becomes the only substrate
- [ ] Leave k3s indefinitely as a co-equal substrate: both substrates are supported; CI matrix tests both; k3s is the answer for headless / server deployments, Claude Code substrate is the answer for solo developers. (default, lowest-risk)
- [ ] Mark k3s 'CI-only': k3s stops being a deployment target for end users (no docs guidance, no images shipped); only remains as a CI environment that runs the conformance suite as a second data point. Removal scheduled for a future cleanup issue
- [ ] Defer the decision: ship substrate-swap, let user feedback drive a follow-up issue that decides k3s's fate after ≥3 months of real-world use of the Claude Code substrate
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-10 -->

**Subagent context-window strategy: how does the substrate handle deep research that would exceed a subagent's context budget (egg targets max_turns: 1000)?**

- [ ] Port egg_container checkpointing: subagents write intermediate findings to .egg-state/checkpoints/; a re-spawned subagent resumes from the checkpoint. Faithful to today's recovery model; requires the Claude Code spawner to re-invoke the agent with checkpoint context
- [ ] Accept smaller-than-1000 turn budget: subagents are bounded by Claude Code's native context limit; role rubrics and the system-prompt depth do most of the work; deep-research breadth is reduced but consistent. Lowest implementation cost
- [ ] Per-agent forked subagent for deep research: a refiner whose context fills up forks a child subagent to do a sub-task ('read all files matching X and summarize'), the child's summary returns to the parent. Mirrors how a human delegates
- [ ] Hybrid: checkpoint for cross-turn recovery, fork for sub-task delegation; the system prompt teaches when to use which
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-11 -->

**Slice-DAG decomposition shape: how should this substrate-swap work be sliced for shippable PRs? (slice count = PR count; siblings in a wave run in parallel)**

- [ ] Single slice — entire substrate swap as one PR family (interfaces + ClaudeCodeSpawner + InProcessMessageBus + PreToolUseHookPolicy + conformance matrix + skill entry point + ADR); 1 PR. Largest blast radius, simplest integration
- [ ] Two-wave parallel: [substrate-interfaces + ADR] -> [ClaudeCodeSpawner || InProcessMessageBus || PolicyEnforcer || WorktreeManager (4 parallel implementations)] -> [conformance matrix + skill entry point]; 7 PRs across 3 waves
- [ ] Three-wave parallel: [interfaces only] -> [each implementation in parallel] -> [k3s side of the interface adapter || conformance matrix || skill entry point || ADR (all parallel)]; ~9 PRs, very parallel
- [ ] Linear chain: interfaces -> message bus -> spawner -> policy -> worktree -> conformance -> skill -> ADR; 8 sequential PRs. Lowest review burden per PR; slowest end-to-end
- [ ] Spike then plan: a single 'walking skeleton' slice that gets one role (refiner) running through one substrate (claude-code) end-to-end, then re-plan the rollout based on what the spike learns; 1 PR for the spike + follow-up issue
- [ ] Other (explain in reply)

<!-- egg-feedback id=feedback-1 -->

## Open-ended feedback

Please **edit the feedback comment on the issue** to answer the following. The same questions are mirrored in the contract's feedback record so they survive cross-phase:

**Q1**: Definition of 'conformance passes on ≥5 representative issues' — should the ≥5 measurement issues be a fixed curated set (and if so, which? — please name them or describe selection criteria), or sampled per-CI-run from open issues, or some other rule?

**Q2**: Are there latency or throughput budgets the Claude Code substrate should meet relative to the k3s substrate? (E.g. 'refine phase ≤ 2x k3s latency'.) If so, what are they? If none, is performance acceptable as 'whatever Claude Code gives us so long as it terminates'?

**Q3**: Are there constraints on third-party dependencies the substrate is allowed to introduce — e.g. any specific Python packages forbidden, any size cap on the marketplace plugin install footprint, any restriction on requiring a developer-mode Claude Code feature flag?

**Q4**: Should the in-process orchestrator support being driven by NON-Claude-Code callers too (e.g. a CLI 'egg-orch local-run --issue 1234' that uses the in-process orchestrator with a non-Claude-Code agent harness)? Or is the Claude Code substrate intentionally Claude-Code-only?

**Q5**: Is there appetite to use this issue to also fix structural-cause #5 from #2622 (no tool-use budget signaling) and #6 (no minimum-breadth targets in role files) — given they require role-file edits that touch shared/prompts/ regardless of substrate — or should those stay scoped to #2622?

**Q6**: Any concerns about telemetry / privacy regression when the orchestrator runs in the user's session vs. a k3s pod (e.g. checkpoints written to local filesystem may contain sensitive prompts/contexts that previously stayed in cluster-storage)?

---

*Authored-by: egg*
