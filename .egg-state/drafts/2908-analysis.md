# Analysis: BRC consensus — deterministic event-pump + durable agent memory

> Issue: #2908 | Phase: refine

## Problem Statement

BRC consensus participation currently depends on the **agent volunteering to
re-enter a blocking wait** between each typed event it must handle. A reviewer's
lifecycle is `wait → review producer A → re-enter wait → review producer B → … →
confirm`; a producer's is `propose → wait → address-NACK → re-wait → confirm →
stay-alive`. Every re-entry is a seam at which the model can emit a final
assistant message and exit `success=True` instead of looping back into
`egg-orch message wait-loop`.

Claude normally re-enters. **qwen3.7-max does not** (issue #2906): it exits
cleanly at ~30–50 of a 1000-turn budget, the orchestrator sees no
`CONSENSUS_CONFIRMED`, and the consensus wrapper restarts the agent. Three
restarts later the pipeline is marked FAILED (issue #2806), burning ~$1 and ~20
minutes per cycle. The lineage of prior fixes — cursor threading (#2323),
pre-confirm-wait foot-gun (#2064/#2482), gap-race (#1995), reviewer
heartbeats (#2036), gateway keep-alive (#2451) — are all artifacts of this same
"agent holds the wait" model. Each fix narrows the seam for one model; none
removes it.

This issue proposes to remove the seam by reframing the consensus agent from a
*persistent participant that holds a wait* into a *stateless per-event handler
the wrapper invokes*, with continuity carried by a **durable distilled memory
file** rather than a live session. The wrapper (deterministic Bash) owns the
blocking wait, and on each actionable event invokes `claude -p` against a warm
pod; the agent reads its prefix + memory + the one event, acts, updates memory,
and exits naturally. The wrapper loops until the orchestrator reports the
role's consensus complete.

The desired outcome is that **no model can stall BRC consensus by exiting
between events** — a property of the control flow, not of any one model's
prompt-following.

## Current Behavior

The relevant primitives the proposal touches are:

**Wrapper / restart loop** — `orchestrator/consensus_wrapper.py`
- `MAX_CONSENSUS_RESTARTS = 3` (line 38). After exhausting restarts the
  wrapper exits 1 and the pipeline is marked FAILED.
- `_RECOVERY_SYSTEM_PROMPT` (lines 64–99) is injected on restart with
  `{restart_number}`, `{max_restarts}`, `{brc_state}`, `{nack_feedback}`,
  `{anchor_state}` placeholders.
- The wrapper already has an SSE event-pump for the "agent confirmed, waiting
  on peers" case (lines 404–504): it subscribes to
  `/api/v1/pipelines/{pid}/stream` and greps for `event: consensus.reached`,
  with a `egg-orch message wait` fallback (lines 507–532) and a sleep-poll
  tertiary fallback. **This pattern is already in production for the
  confirmed-and-waiting path; the proposal generalises it to the whole BRC
  lifecycle.**
- Per-restart `OVERSEER_ALERT` (lines 570–585, issue #2806) so the operator
  sees recovery attempts in real time.
- `build_consensus_wrapped_command` (line 720) is consumed by
  `orchestrator/concurrent_executor.py:37` (production) and
  `orchestrator/routes/pipelines.py:2792`.

**Wait primitive** — `sandbox/egg_agent_tools/handlers/message.py`
- `message_wait_loop()` (line 267) blocks through timeouts, threads
  server-side cursor via `since_id` (issue #1995), and returns on the **first**
  matching event (`if resp.get("matched"): return resp_out`, line 405). It
  emits `WAITING_FOR_EVENT` heartbeats every 60s
  (`_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS`, line 47) carrying `slice_id` for
  gateway session keep-alive (#2451). On exit it emits a final `WORKING`
  heartbeat.
- The heartbeat-emitting daemon thread is started by
  `_start_wait_loop_heartbeat()` and torn down in the `finally` block (lines
  421–432).
- Cursor persistence lives in `/tmp/egg-wait-cursor-${EGG_PIPELINE_ID}-${EGG_AGENT_ROLE}-*`
  per issue #2323.

**Agent-facing MCP tool surface** — `sandbox/egg_agent_tools/tools/` (11
modules, ~31 tools) wraps a shared handler layer in `handlers/` (11 modules).
The handlers are also invoked by the `egg-orch` and `egg-contract` CLIs, so
~18 of 28 tools already have CLI parity per
`tests/tools/test_mcp_cli_drift.py`. The tools registered without CLI peers
include `brc__get_state`, `brc__list_blocking`, `phase__get_context`,
`brc__resolve_obligation`, `brc__read_peer_artifact`. The MCP server is
registered at `sandbox/egg_agent_tools/server.py`; integration coverage lives
at `integration_tests/test_sandbox_mcp_tools_e2e.py` and
`tests/sandbox/egg_agent_tools/test_server.py`.

**Role write-scoping** — `shared/egg_restrictions/patterns.py`
- Every producer and reviewer role can write `.egg-state/agent-outputs/`,
  including REFINER (lines 488–507), REVIEWER_REFINE (lines 509–514) and
  REVIEWER_AGENT_DESIGN (lines 479–484). A new `agent-outputs/<role>/brc-memory.md`
  artifact would land within the existing allowlist without restriction
  changes. Enforcement is by `tool_interceptor.check_file_write_permission`
  in-pod plus `phase_filter.validate_agent_push` at the gateway.

**Pod & worktree isolation** — `orchestrator/kubernetes_spawner.py`
- One Job per `(pipeline_id, role)` (slice-scoped:
  `egg-agent-{pipeline_id}-{slice_id}-{role}`). Pods are warm for the
  agent's lifetime, so successive `claude -p` invocations from the wrapper
  reuse the same pod, the same worktree, the same `EGG_AGENT_ROLE`, and the
  same per-role MCP/handler permissions.

**Provider cache** — `config/litellm/patch_litellm_cache.py`
- Patches openrouter/anthropic adapters for Qwen/DeepSeek cache support.
  Does not surface a `cache_read_input_tokens` aggregate to egg today; reading
  per-call provider cache hits requires parsing OpenRouter response usage or
  the `~/.local/state/clm/cost-*.json` callback. **The WS7 follow-up comment
  on the issue confirms the Anthropic route caches across separate
  processes (1h TTL, ~92% prefix read) — so the stateless-process
  invariant is already proven for that route. The Qwen route is unverified
  and is the WS0 spike gate.**

**Liveness signals** — heartbeats currently originate from
`message_wait_loop`'s daemon thread (line 47); migrating to wrapper-emitted
heartbeats requires the wrapper to know `(pipeline_id, role, slice_id, state)`
which the env already exposes.

## Constraints

- **No change to the agent primitive.** Pod, worktree, role-scoping, SDK
  choice, gateway, and permissions all stay as-is. The change is the *shape
  of the agent's session*, not the agent itself.
- **Gateway-enforced role boundaries persist per-invocation.** Each `claude -p`
  call inherits the pod's `EGG_AGENT_ROLE` and the gateway's push filter, so
  no per-event re-authentication or boundary re-check is needed.
- **Prose-bearing CLI invocations must take text via stdin or a file path,
  never argv.** Issue #2741 mitigated `bash -c`'s shell-metachar corruption
  of `consensus propose --summary`, `nack --reason`, and `--files-reviewed`
  by routing through the MCP server. If WS8 collapses those tools to CLI,
  the prose arguments must use `--reason-file` / `--summary-file` / stdin
  to avoid reintroducing the regression.
- **Anthropic-route cross-process caching is measured-clear** (issue WS7
  comment, two independent runs). The proposal's "stateless = no cache
  benefit" objection does not apply on the Anthropic route. **Qwen-route
  provider-cache TTL is unmeasured** and is the WS0 stop/go gate.
- **No new harness.** The proposal explicitly avoids a persistent-streaming
  agent process and uses SDK-native one-shot `claude -p`. A tail
  cache-breakpoint (to cache accumulated work, not just prefix) likely needs
  prompt-construction control `claude -p` does not expose; if work-caching
  becomes required, that constraint conflicts with the "no custom harness"
  pillar.
- **Backwards compatibility for in-flight pipelines.** The capped-restart
  wrapper path must remain reachable behind a flag until the new path is
  validated, so cohorts that started on the old path can finish on it.
- **Prior-fix preservation.** The migration must keep working: cursor
  threading (#2323), pre-confirm-wait rejection (#2064/#2482), the gap-race
  fix (#1995), heartbeat liveness (#2036), gateway keep-alive (#2451),
  open-NACK aggregation barrier (#2142), conditional-ACK / stale-version
  re-review (#2482), and the producer-allowlist scope (#2725).
- **Advisory seam information for the planner**: the change touches the
  **orchestrator** (`consensus_wrapper.py`, possibly new `consensus
  next-action` endpoint, `_build_brc_preamble` in
  `routes/pipelines.py`), the **sandbox** (handlers/tools restructuring,
  optional MCP→CLI collapse, mission.md), **shared** (BRC prompt
  templates), **config/litellm** (cache instrumentation for the Qwen route
  spike), and **tests** (handler/CLI tests replace MCP-server tests if WS8
  lands). The planner is free to slice this differently.

## Options Considered

### Option A: Status-quo + prompt hardening only

**Approach**: Strengthen the BRC preamble and STAY-ALIVE instructions; tune
the recovery system prompt; possibly add per-model preamble variants. Keep
the agent-held wait, keep the 3-restart cap.

**Pros**:
- Zero blast radius; touches only prompt strings.
- No risk of regressing the prior fix lineage.

**Cons**:
- Per-model patch; the next provider (DeepSeek, future routes) will need
  another round of prompt tuning.
- The structural seam — model volunteering to re-enter a wait — is
  unchanged. Qwen-class failures recur with any model whose RLHF prefers
  clean exits.
- Does nothing about the per-cycle cost of restarts when the patch fails.

### Option B: Stateless event-pump + durable distilled memory (the issue's proposal)

**Approach**: Wrapper blocks on `egg-orch message wait-loop`, invokes
`claude -p` on the warm pod per actionable event. Agent loads cached prefix +
durable memory file + the one event, acts, writes memory, exits. Wrapper
loops until orchestrator reports role consensus complete.

**Pros**:
- Removes the structural seam — a deterministic loop drives waits and
  termination; no model can fall out of it.
- Converges consensus execution toward egg's existing one-shot agent model;
  net deletion of the wait machinery, recovery-system-prompt, SSE event-pump
  generalisation already partly present in the wrapper, and (with WS8) the
  28-tool MCP surface.
- Prefix caching is preserved on the Anthropic route (WS7 measured); the
  Qwen route is gated by WS0.
- Token-bounded per event (memory + delta), not unbounded transcript growth.
- The wrapper's existing SSE+wait-loop infrastructure (lines 404–532 of
  `consensus_wrapper.py`) is the foundation — generalising it is a smaller
  jump than building a new control plane.

**Cons**:
- Memory-curation reliability becomes a new failure mode. Mitigated by
  action-scaffolding writes off `brc_ack`/`brc_nack` (which already carry
  `reason` + `files_reviewed`) rather than free-form journaling, with
  orchestrator message history as the reconstruction backstop.
- WS7 (cache) remains genuinely open on the Qwen route. The WS0 spike must
  prove cross-invocation cache hits land against the provider cache; a fail
  here either narrows v1 scope to the Anthropic route or forces a keep-warm
  loop on the Qwen route.
- WS8 (MCP→CLI collapse) is a large workstream that compounds blast radius.
  The "no harness" pillar partly hinges on it — a stateless one-shot
  process is the wrong shape for an in-process MCP server — but the core
  event-pump does not strictly require WS8 to land at the same time.
- Per-event CLI invocation latency (WS8 only) is unmeasured; tested
  fallback is a persistent `egg-orch` daemon socket or call batching.

### Option C: Persistent agent session with SDK `--resume`

**Approach**: Keep the agent process alive across "events" using
`claude --resume`. The wrapper still owns the wait, but instead of
re-invoking `claude -p` each event, it sends new turns to the same session
via SDK resume.

**Pros**:
- Continuity is natural — no externalised memory artifact needed for the
  tightly-coupled NACK→re-review thread.
- Lower per-event setup cost than spawning fresh processes.

**Cons**:
- Still requires an out-of-band signal mechanism for the wrapper to push
  events into the resumed session — re-introduces a custom harness boundary
  the issue explicitly rules out.
- `--resume` does not solve the Qwen exit problem on its own: a resumed
  session still relies on the model to stay engaged across turns. If the
  control loop is in the wrapper anyway, the only marginal benefit of
  `--resume` over fresh-+-memory is cheaper continuity for a single short
  thread.
- Less alignment with egg's existing one-shot agent model. Net-add of code
  rather than net-removal.

### Option D: External watchdog re-invokes a fallen-out agent

**Approach**: Keep agent-held wait. Have an out-of-band watchdog (in the
wrapper or orchestrator) detect when an agent exits without consensus and
re-spawn it with recovery context. This is essentially the current
3-restart cap but lifted from "wrapper" to "control-plane".

**Pros**:
- Smallest net change.

**Cons**:
- Trades one fragile model-driven loop for another fragile recovery loop.
- Still depends on the model to re-enter the wait on re-spawn; Qwen's
  failure mode is precisely that re-spawn does not change behaviour.
- Does not reduce the per-restart cost (~$1, ~20 min).

## Recommended Approach

**Option B (stateless event-pump + durable distilled memory)** with two
boundary qualifications surfaced for the operator (see Open Questions):

1. **WS0 (de-risking spike) is the load-bearing decision.** The cross-process
   cache result on the Anthropic route is already measured-clear; the WS0
   spike's job on the Qwen route is to either replicate that result against
   the OpenRouter/Alibaba provider cache or quantify the cold-read cost so
   that WS7's keep-warm cadence has a budget. The spike outcome should gate
   whether v1 ships with the Qwen route enabled, with the Anthropic route
   only, or behind a per-route flag.

2. **WS8 (MCP→CLI collapse) is the largest discretionary sub-scope.** The
   core event-pump can land without WS8 — the agent can still call MCP
   tools per event — but the "one-shot process should have one way to act"
   argument is real and the deletion footprint is large. Splitting WS8 to
   a follow-up keeps the core change reviewable; landing it together
   collapses the prompt and the action surface in one pass.

Option B is recommended because it is the only option that removes the
structural seam rather than narrowing it. The wrapper-as-event-pump pattern
is already partly in production (SSE consensus-reached loop, lines 404–532
of `consensus_wrapper.py`); the proposal generalises a known-working
pattern rather than introducing a new one. The Anthropic-route cache
property — proven empirically in the issue's WS7 follow-up — turns the
biggest theoretical objection ("stateless will be expensive") into a
non-issue on egg's baseline route.

**Runtime primitives the plan will depend on** (surfaced explicitly for the
plan-phase Primitive-Existence and Trust-Boundary audits):

| Primitive | Where | Context |
|---|---|---|
| `claude -p` SDK one-shot mode | `sandbox` (already used in `sandbox/egg_lib/gha_exec.py:101`) | in-sandbox-agent |
| `egg-orch message wait-loop` CLI | `sandbox/egg_lib/orch_cli.py` | wrapper-side, in-pod |
| `egg-orch consensus status --json` | `sandbox/egg_lib/orch_cli.py:2783` | wrapper-side and agent-side |
| `egg-orch consensus propose/ack/nack/confirmed` | same | agent-side, prose-bearing (constraint above) |
| `_RECOVERY_SYSTEM_PROMPT` template | `orchestrator/consensus_wrapper.py:64–99` | to be replaced by lean event-handler contract |
| `MAX_CONSENSUS_RESTARTS = 3` | `orchestrator/consensus_wrapper.py:38` | to be replaced by an idle/no-progress safety budget |
| `_build_brc_preamble` | `orchestrator/routes/pipelines.py:12348` | prompt collapse (WS6) target |
| `_WAIT_LOOP_HEARTBEAT_INTERVAL_SECS` (60s) | `sandbox/egg_agent_tools/handlers/message.py:47` | heartbeat migration source |
| `resolve_slice_id(req)` | same | heartbeat-side helper to preserve in the wrapper migration |
| `.egg-state/agent-outputs/<role>/brc-memory.md` | new artifact; `shared/egg_restrictions/patterns.py` (already in every role's allowlist) | in-sandbox-agent writes; in-sandbox-agent reads on next invocation |
| `tool_interceptor.check_file_write_permission` | sandbox | per-invocation enforcement (unchanged) |
| `phase_filter.validate_agent_push` | gateway | per-push enforcement (unchanged) |
| 28 agent-facing MCP tools | `sandbox/egg_agent_tools/{tools,handlers}/*.py` | WS8 removal targets (optional) |
| `consensus.reached` SSE event-name | `/api/v1/pipelines/{pid}/stream` | wrapper-side, already in production for the confirmed-and-waiting case |
| `~/.local/state/clm/cost-*.json` callback | `config/litellm/patch_litellm_cache.py` | spike-only, for Qwen provider-cache instrumentation |
| `EGG_AGENT_ROLE` / `EGG_PIPELINE_ID` / `EGG_SLICE_ID` | env, wrapper-injected | per-invocation context |

The change touches the **orchestrator** (wrapper, optional `consensus
next-action` endpoint, BRC preamble), the **sandbox** (handlers/tools and
mission.md), **shared** (BRC prompt templates), **config/litellm** (Qwen
spike instrumentation), and **tests** (handler-direct tests if WS8 lands).
**This seam list is advisory** — the planner may slice differently.

## Open Questions

### Resolved in Pre-Refine

(None — the issue body itself enumerates 6 "Open decisions (resolve during
implementation, several against spike data)". Those are explicitly
implementation-strategy decisions for the planner/coder against measured
data; they are not registered here. The WS7 follow-up comment resolves the
Anthropic-route cache question empirically.)

<!-- egg-hitl-decision id=cq-1 -->

**Should WS8 (collapse the 28 agent-facing MCP tools to CLI) land in this issue, or be split to a follow-up after the core event-pump is proven?**

- [ ] Land WS8 in this issue (collapses prompt + action surface in one pass; larger blast radius but more deletion)
- [ ] Split WS8 to a follow-up (smaller core change; ships event-pump first, MCP collapse after the new control flow is validated in production)
- [ ] Land WS8 partially in this issue (only the net-new CLI commands needed by WS3 — get-state, list-blocking, get-context — and the deletion of MCP tools with existing CLI parity; defer prose-bearing propose/ack/nack collapse to a follow-up so the #2741 stdin/file-path rule can be designed separately)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-2 -->

**If the WS0 spike shows the Qwen-route provider cache TTL is materially worse than the Anthropic route, how should v1 ship?**

- [ ] Ship v1 with the Qwen route enabled and a keep-warm loop sized to the measured provider-cache TTL (full route parity at the cost of a recurring keep-warm token spend)
- [ ] Ship v1 with the Qwen route gated off behind a per-route flag (Anthropic route gets the durable fix immediately; Qwen route stays on the old capped-restart path until keep-warm or a provider TTL change makes it economical)
- [ ] Block v1 on Qwen-route cache validation (no ship until the Qwen route's per-event cost is within the operator's per-cycle budget; treat the Anthropic route's measured-clear status as not enough to justify a partial rollout)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-3 -->

**What is the desired terminal state when the new safety budget (replacing the 3-restart FAIL cap) is exhausted?**

- [ ] OVERSEER_ALERT + HITL decision (current behaviour preserved: operator picks resume/abort, no automatic FAIL)
- [ ] OVERSEER_ALERT only; pipeline keeps running indefinitely (operator must intervene to stop it; suits the 'no-progress' interpretation where there is nothing wrong, just nothing happening)
- [ ] Hard FAIL after a configurable wall-clock cap (preserves the #2806 'producer permanent death → FAILED' semantics but on time rather than restart count)
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=cq-4 -->

**How long should the old capped-restart wrapper path be retained behind a flag after the new event-pump path lands?**

- [ ] Until the next release tag, then delete (forcing-function for full migration; smallest carrying cost)
- [ ] Until N successful pipelines complete on the new path (e.g. 50) without any new restart-related alerts, then delete (data-driven)
- [ ] Indefinitely as a configurable opt-out (operator can always escape-hatch to the legacy path; largest carrying cost but lowest risk)
- [ ] Other (explain in reply)

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: Are there operator-side budget caps (per-pipeline token spend, per-event wall-clock, keep-warm-loop $/hour) that should be expressed as hard fail conditions for the WS0 spike rather than just instrumentation? If yes, name the threshold.**

> _Your answer here_

**Q2: Is the new memory artifact (.egg-state/agent-outputs/<role>/brc-memory.md) considered authoritative audit material that must be preserved post-pipeline, or ephemeral coordination state that can be cleaned up with the pod? (Affects retention policy and gateway treatment.)**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

## Complexity Assessment

**high** — fundamental reframe of consensus-agent execution (stateless
handler + externalised memory). Touches the wrapper, the BRC preamble, the
sandbox handler/tool layer (with optional MCP→CLI collapse), liveness
emission, and cache instrumentation across orchestrator, sandbox, shared,
and config/litellm. The agent primitive (pod/worktree/role/SDK/permissions)
is **not** changed; blast radius is contained to the consensus subsystem
but is deletion-heavy and crosses multiple subsystem boundaries.

---

*Authored-by: egg*
