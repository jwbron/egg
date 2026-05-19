# Claude Code Substrate (#2623 spike → #2717 rollout)

> Status: **rollout in progress, interfaces still unstable** — the spike for [#2623](https://github.com/jwbron/egg/issues/2623) landed the four substrate `Protocol`s with one role (refiner) exercising them end-to-end on the Claude Code side and a thin `K3sSpawnerAdapter` shim keeping `EGG_SUBSTRATE=k3s` green. The rollout under [#2717](https://github.com/jwbron/egg/issues/2717) extends the substrate slice by slice. **Slice 1 (refine-team expansion + flattened HITL bridge + R2 spike) has landed**; slices 2–5 (plan / implement / pr / hardening) are pending. The interfaces still carry the `# v0.x — unstable until ≥3 roles exercise` marker (R10); the marker drops in slice 5. The full conformance matrix, the real k3s interface adapter, and the cost-cap / prune / fork primitives are tracked in the [Rollout deltas](#rollout-deltas) section below.

This ADR documents the substrate-swap landed by issue [#2623](https://github.com/jwbron/egg/issues/2623): a parallel Claude-Code-native execution substrate for the egg SDLC stack, sitting behind four named `typing.Protocol`s in `orchestrator/substrate/` and selected at boot by an `EGG_SUBSTRATE` env var. The k3s substrate (`KubernetesSpawner`, `RedisMessageStore`, gateway sidecar) keeps working unchanged; the Claude Code substrate is opt-in.

The walking-skeleton scope was settled by the refine-phase HITL: **cq-11 = "Spike then plan"**. One slice, one role, end-to-end on the new substrate, then re-plan the rollout in a follow-up issue once the spike has returned real numbers on subagent context behavior, hook ergonomics, and `Agent` tool throughput.

## Why a substrate swap, not a parallel skill

PR #2608 shipped `plugins/refine-plan/` — a Markdown skill that *approximates* egg's refine + plan phases using Claude Code subagents and a filesystem verdict journal. Child issues #2612 (concurrent BRC) and #2622 (depth gap) catalogued the structural shortcomings of that approximation: iterated rather than concurrent, no real message bus or version tracking, no mid-cycle revision, no `build_system_prompt` depth, reviewer rubrics that don't enforce evidence breadth.

This issue takes a different framing. Rather than chase parity with a parallel Markdown implementation, **swap the substrate underneath the real stack**: keep `egg_orchestrator`, `egg_harness`, `egg_contracts`, `egg_agent`, and `shared/prompts/` unchanged, and re-platform them onto Claude Code's native primitives. If the swap is real (versus an approximation), depth, BRC mechanics, contract schema, role prompts, and HITL semantics come for free from the reused upstream code. **Quality becomes structurally inevitable**: the only question is "does the substrate-swap layer faithfully expose the orchestrator's coordination surface to a Claude Code session?"

| Today's substrate | Claude-Code-native replacement |
|---|---|
| **k3s** schedules agent pods (`KubernetesSpawner`) | Claude Code's `Agent` tool spawns subagents; `ClaudeCodeSpawner` dispatches via `shared/egg_harness` |
| **Docker container** per agent (`sandbox/Dockerfile`) | `Agent` tool's `isolation: "worktree"` + Bash sandbox mode |
| **Redis Streams** message bus (`RedisMessageStore`) | In-process Python (`InProcessMessageBus`) — reuses the existing in-memory `MessageStore` |
| **Gateway sidecar** policy + creds (`gateway/`) | PreToolUse hooks in `.claude/settings.json` calling into the existing `shared/egg_restrictions/patterns.py` |
| **Persistent volume** for worktrees | User's local filesystem (`~/.egg-worktrees/<pipeline_id>/<role>/` by default via `LocalWorktreeManager`; `EGG_WORKTREE_BASE` overrides) |
| **Sandbox container image** | User's local Claude Code install + per-role `agents/*.md` files |
| **`kubectl get pods` health checks** | In-process `egg_health` thread |
| **Overseer pod** | In-process overseer thread |

This is **cq-1 = parallel substrates, env-var-selected (Option A)**, executed via cq-11's spike-first sequencing.

## The eleven cq decisions, in one table

The refine-phase HITL settled eleven decisions and six feedback items. Each one shapes a piece of the substrate seam, named below alongside the module it lands in.

| Decision | Selection | What lands |
|---|---|---|
| **cq-1** substrate strategy | Option A — parallel substrates, env-var-selected | `orchestrator/substrate/` with `select_substrate(env)` factory reading `EGG_SUBSTRATE` |
| **cq-2** parent-close phase scope | All phases (refine + plan + implement + pr) | Spike landed refine-only per cq-11. **Slice 1 of #2717 expands the refine roster** to the full refine-team (refiner + reviewer_refine + reviewer_agent_design). Slices 2 / 3 / 4 land plan / implement / pr |
| **cq-3** conformance scoping | Extend `integration_tests/regression/` with a `substrate` parameter (CI matrix) | One regression test parametrized via the new fixture in this spike; full matrix factor-out deferred |
| **cq-4** spawner shape | Synchronous `spawn(role, prompt, env, worktree) → AgentResult` | `AgentSpawner` protocol pinned at this signature |
| **cq-5** worktree ownership | Port `WORKTREE_BASE_DIR` model | `LocalWorktreeManager` mirrors `gateway/worktree_manager.py:49` shape; per-agent worktrees land at `<base>/<pipeline_id>/<role>/`; default `<base>` is `~/.egg-worktrees/`, `EGG_WORKTREE_BASE` overrides (typical override: `./.egg-state/`) |
| **cq-6** policy seam | PreToolUse hooks | `PreToolUseHookPolicy` ships a hook entry script + `settings.template.json`; calls the existing `shared/egg_restrictions/patterns.py:768 build_agent_patterns` |
| **cq-7** HITL surface | Heredoc-style synchronous generator | `run_pipeline_in_process(...)` is a generator yielding `HITLDecision` objects; the skill renders each via `AskUserQuestion` and resumes via `.send(...)`. **Bridge: flattened (Option C, refine/plan) + daemon (Option C, implement).** Slice 1 of #2717 closes the refine-phase bridge gap via `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py` — a single-yield stage driver that round-trips each `HITLDecision` through `.egg-state/contracts/<id>.json#pending_hitl`. Slice 3 of #2717 ships the daemon variant for implement-phase concurrency. The `pending_hitl` envelope is a shared state-serialization contract between the two bridges (risk_analyst R17 mitigation). |
| **cq-8** packaging | Plugin metadata declares pip dependencies | **Deferred to follow-up** (depends on cq-12). `plugins/egg-sdlc/.claude-plugin/plugin.json` currently carries an `egg.install_instructions` from-source command (`git clone … && pip install -r requirements.txt && export PYTHONPATH=…`) as the operator-actionable surface — see the [`egg-sdlc plugin`](#the-egg-sdlc-plugin) section at line 122. Resolving cq-12 swaps this back to a pip-dep declaration. |
| **cq-9** k3s disposition | Leave indefinitely | k3s code untouched in this spike; deprecation is a future-issue question |
| **cq-10** context-window strategy | Hybrid — checkpoint + fork | Spike ports the checkpoint half; forking is deferred to follow-up (it's a quality booster, not a correctness requirement) |
| **cq-11** slice shape | Spike then plan | One slice in the spike; #2717 is the follow-up planning issue with a 5-slice DAG |
| **cq-12** canonical pip name | **Deferred to follow-up** | Operator-decidable scope; `plugins/egg-sdlc/.claude-plugin/plugin.json` carries `egg.install_instructions` (from-source command) as the operator-actionable surface until cq-12 settles. See the [`egg-sdlc plugin`](#the-egg-sdlc-plugin) section at line 122. |

### Feedback applied

- **Q1** (conformance set): spike runs against **one** fixed curated issue; follow-up extends to 5. Selection rule documented in the follow-up draft.
- **Q2** (latency budget): explicitly deferred — no perf gate in the spike's acceptance criteria; follow-up captures budget once real numbers exist.
- **Q3** (deps): spike pins to existing egg pip deps; no new third-party dependencies; no developer-mode Claude Code feature flag required; marketplace footprint stays well under the soft ~100 MB cap.
- **Q4** (non-Claude-Code callers): secondary goal. Interfaces are designed to admit an `EggHarnessSpawner` later (subprocess-driven `egg_harness` for headless CLI). The spike does not build it.
- **Q5** (#2622 structural causes #5/#6): out of scope for this issue; stays with #2622.
- **Q6** (telemetry/privacy): checkpoints are local-filesystem-only (`.egg-state/checkpoints/<pipeline_id>/`); no telemetry sent. Spike does not ship a prune verb — reserved for the follow-up.

## The four interfaces

All four interfaces live under `orchestrator/substrate/` as `typing.Protocol`s and carry the `# v0.x — unstable until ≥3 roles exercise` marker in their module docstrings (R10). The k3s side gets a working `K3sSpawnerAdapter` shim from day one so `select_substrate({})` returns a working spawner — the cq-1 parallel-substrates choice rules out raising `NotImplementedError` for the k3s leg.

### `AgentSpawner` — `orchestrator/substrate/spawner.py`

cq-4: synchronous `spawn(role, prompt, env, worktree) → AgentResult`. The caller blocks until the agent completes. Internal concurrency is owned by the spawner, so the orchestrator's existing `ThreadPoolExecutor` in `orchestrator/concurrent_executor.py:114` keeps issuing parallel `spawn()` calls without changes. `AgentResult` is a dataclass with `stdout`, `exit_code`, `duration_seconds`, `worktree`, **and `commit_sha: str | None`** — the SHA is required so reviewers can attach commit-bound ACKs per the existing INV-6 invariant in `orchestrator/action_guards.py:631` (invariant body at `:757`).

- **k3s implementation**: `K3sSpawnerAdapter` (in `orchestrator/substrate/k3s_adapter.py`) wraps `create_concurrent_spawn_fn` (`orchestrator/kubernetes_spawner.py:1564`). It returns `AgentResult.commit_sha=None` by design: the legacy factory is fire-and-monitor, so a `git rev-parse HEAD` on the orchestrator host at adapter-return time would capture the *pre*-spawn HEAD and BRC reviewers would attach commit-bound ACKs to the wrong SHA. The legitimate INV-6 SHA for k3s is supplied through the existing gateway-side attestation channel that reads it off `SpawnedContainer.container_info` after the pod terminates. Plumbing that channel into the protocol's `commit_sha` field directly is tracked in the [Rollout deltas](#rollout-deltas) ("wire gateway attestation into `AgentResult.commit_sha`"). No behavior change for k3s users — the dispatch seam at `_spawn_agent` is gated on `EGG_SUBSTRATE=claude-code` only and the legacy path remains in place for unset / `"k3s"` (reviewer v1 blocker #1).
- **Claude Code implementation**: `ClaudeCodeSpawner` (in `orchestrator/substrate/claude_code/spawner.py`) blocks the caller, dispatches to Claude Code's `Agent` tool surface via `shared/egg_harness`, and runs `git -C <worktree> rev-parse HEAD` immediately after the subagent returns to capture `commit_sha`. It assembles the per-role system prompt via `build_system_prompt(sources)` (`shared/egg_harness/prompt.py:24`) — this is the structural depth fix from #2622: by routing through the real prompt assembler, all four depth-gap structural causes close as a side-effect of running the real harness in a Claude Code session.

The dispatch seam at `orchestrator/concurrent_executor.py:504 _spawn_agent` is patched to invoke `select_substrate(os.environ).spawner.spawn(...)` **only when `EGG_SUBSTRATE=claude-code` is set explicitly**. Unset or `"k3s"` preserves the legacy `self.spawn_fn(...)` path verbatim (reviewer v1 blocker #1 — until `K3sSpawnerAdapter` forwards slice-aware branches and the BRC consensus-wrapped command, setting `EGG_SUBSTRATE=k3s` would silently lose both). The follow-up issue extends the adapter and re-opens the seam to k3s once the gaps close.

### `MessageBus` — `orchestrator/substrate/message_bus.py`

The BRC-mechanics object the orchestrator drives. Implementations must preserve INV-3 (stale-version rejection) and INV-5 (open-NACK barrier) from `orchestrator/action_guards.py:631 validate_invariants` — these are the BRC concurrency invariants that survive substrate transitions per the issue body's "if we use the real orchestrator in-process, BRC mechanics come for free."

- **k3s implementation**: backed by `RedisMessageStore` (`orchestrator/redis_message_store.py:107`). Untouched by this spike.
- **Claude Code implementation**: `InProcessMessageBus` (in `orchestrator/substrate/claude_code/message_bus.py`) uses Python `dict` / `threading.Lock` / `queue` primitives. It may subclass or delegate to the existing in-memory `MessageStore` (`orchestrator/message_store.py:200`); the BRC test suite at `orchestrator/tests/test_brc_*.py` is the behavioral oracle the bus must match.

### `PolicyEnforcer` — `orchestrator/substrate/policy.py`

The gateway-equivalent for file-write and tool-use restrictions. cq-6 picked PreToolUse hooks as the primary enforcement seam.

- **k3s implementation**: the existing gateway's `check_agent_restrictions` (`gateway/phase_filter.py:1061`) is the reference. The spike does not adapt this onto the new protocol — that's a follow-up task (R2 fallback path below).
- **Claude Code implementation**: `PreToolUseHookPolicy` (in `orchestrator/substrate/claude_code/policy.py`) plus a hook entry script (`hook_entry.py`) referenced from a `.claude/settings.json` template (`settings.template.json`). The hook reads tool name + tool input from stdin per the Claude Code PreToolUse contract, imports `build_agent_patterns` from `shared/egg_restrictions/patterns.py:768`, and emits `deny` + `message` JSON to stdout when the write target lands outside the caller's role's allow-list. **Single source of truth**: the hook calls the *exact same* `build_agent_patterns` symbol the gateway uses — no parallel restriction logic.

### `WorktreeManager` — `orchestrator/substrate/worktree.py`

cq-5: port the existing `WORKTREE_BASE_DIR` model rather than use Claude Code's native `EnterWorktree` primitive. The substrate creates per-agent worktrees in a `<pipeline_id>/<role>/` subdirectory under a single configurable base, tracks them in a dict, and tears them down at phase end. (The path keys on `role`, not `repo`, because the in-process orchestrator runs against a single repo per pipeline and the worktree's per-role isolation is what matters.)

The default base is **`~/.egg-worktrees/`** — matching the shape of `gateway/worktree_manager.py:49 WORKTREE_BASE_DIR` (which hardcodes `/home/egg/.egg-worktrees` for the gateway container; the Claude-Code-substrate implementation expands `~` against the calling user's `$HOME` instead). `EGG_WORKTREE_BASE` overrides the base — typical override is `./.egg-state/` so worktrees live alongside the contract / drafts / agent-outputs files in the same `.egg-state/<pipeline_id>/` tree, per cq-5's literal text. Full path on disk by default: `~/.egg-worktrees/<pipeline_id>/<role>/` (one worktree per role); under the typical override: `.egg-state/<pipeline_id>/<role>/`. The branch name created in each worktree follows `egg/<pipeline_id>/<role>` for legibility in `git branch` output.

- **k3s implementation**: the existing `gateway/worktree_manager.py` already implements this shape (default base at `gateway/worktree_manager.py:49`, hardcoded to `/home/egg/.egg-worktrees`). The k3s adapter is not implemented in this spike — left as a TODO in the protocol module.
- **Claude Code implementation**: `LocalWorktreeManager` (in `orchestrator/substrate/claude_code/worktree.py`) defaults to the same `~/.egg-worktrees/` base shape but respects an `EGG_WORKTREE_BASE` override. Path-escape safety mirrors the `is_relative_to` + `resolve()` defense at `gateway/worktree_manager.py:1700-1711` (call site within `list_orphan_worktree_dirs`, defined at `:1687`). The bug class is identical to worktree teardown — symlink-traversal attempts must be rejected before any filesystem mutation.

## `EGG_SUBSTRATE` and `select_substrate(env)`

The factory function at `orchestrator/substrate/__init__.py` reads `EGG_SUBSTRATE` from the environment and returns a substrate bundle:

| `EGG_SUBSTRATE` | Returns | Notes |
|---|---|---|
| unset, `""`, or `"k3s"` | `SubstrateBundle(spawner=K3sSpawnerAdapter, ...)` | The default; existing k3s deployments stay green |
| `"claude-code"` | `SubstrateBundle(spawner=ClaudeCodeSpawner, bus=InProcessMessageBus, policy=PreToolUseHookPolicy, worktrees=LocalWorktreeManager)` | Opt-in claude-code path |
| any other value | raises | Misconfigured env should fail loudly, not silently fall back |

## The in-process orchestrator: `run_pipeline_in_process(...)`

The spike's most expensive task. Today the orchestrator is a Flask + waitress HTTP daemon (`orchestrator/cli.py:83 cmd_serve`) with `ConcurrentPhaseExecutor` (`orchestrator/concurrent_executor.py:114`) running its own `ThreadPoolExecutor` and `PeerConsensusTracker` (`orchestrator/peer_consensus.py:69`) holding its own locks. The in-process boot path is **net-new** to the claude-code substrate — `egg-orch` remains a thin HTTP client for the k3s side.

`run_pipeline_in_process(...)` (in `orchestrator/substrate/in_process.py`) is a Python generator that yields `HITLDecision` objects (`orchestrator/models.py:300`) when the pipeline pauses for a human decision. Per cq-7 = heredoc-style synchronous, the skill renders each yielded decision via `AskUserQuestion` and resumes via `generator.send(answer)`.

### The flattened bridge (Option C, refine + plan — landed in slice 1 of #2717)

A Claude Code skill cannot drive a long-lived Python generator across multiple `AskUserQuestion` round-trips natively — `AskUserQuestion` is a tool the LLM calls, not a function callable from a `python3` subprocess, and every `python3` invocation from a Bash skill step is a fresh process whose `gi_frame` dies at exit. **Per cq-1 = hybrid (Option C)**, the rollout picks the *flattened* approach for refine and plan phases and a *daemon* variant for implement phase:

- **Flattened (slice 1, refine + plan).** The skill loops over invocations of `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py`. Each invocation loads `.egg-state/contracts/<id>.json`, promotes the operator's most recent `pending_hitl.answer` into `pending_hitl.answer_log`, spawns a fresh `run_pipeline_in_process(...)` generator, **replays the entire `answer_log`** into it to reach the next un-answered yield (the flattened bridge is deterministic-replay-based, not single-step resumption — fresh process every call), serialises the yielded `HITLDecision` into `pending_hitl.decision`, and exits 0 with `pending_hitl.status = "pending"`. The skill renders the decision via `AskUserQuestion`, writes the operator's selection back to `pending_hitl.answer` + `status = "answered"`, and re-invokes the driver. On `StopIteration`, the driver clears `pending_hitl.decision` to `None`, sets `status = "completed"` (or `"aborted"` if the last answer was an abort), and writes the generator's return value (typically the analysis path) to `pending_hitl.result`. The skill loop's exit predicate is `status ∈ {completed, aborted, error}`.
- **Daemon (slice 3, implement).** A long-lived Python REPL the skill talks to via a JSON-RPC envelope, so the generator state survives the multi-producer concurrency of implement-phase BRC (and replay-based fast-forward becomes prohibitively expensive). The daemon variant consumes the **same 9-field `pending_hitl` envelope shape** the flattened driver writes — `version`, `pipeline_id`, `timestamp`, `decision`, `answer`, `status`, `result`, `error`, `answer_log` — risk_analyst R17 mitigation. A pipeline started on the flattened bridge can be resumed on the daemon variant and vice-versa; the daemon variant simply skips the replay step because its generator survives across invocations.

The full 9-field `pending_hitl` envelope is a stable cross-bridge contract. The flattened driver's top-of-file comment at `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py:20-46` is the source of truth; the daemon variant in slice 3 (TASK-3-2) consumes the same shape. SKILL.md mirrors the schema in its "How the flattened bridge works" section.

Key properties (unchanged from the spike):

1. **Heartbeat-during-HITL** within an invocation: while the generator is paused at a yield boundary inside a single `bin/run_pipeline.py` invocation, the in-process orchestrator's background threads (heartbeat poll, BRC re-review, message-bus tick) continue to run so a long-paused HITL does not cause stuck-phase-transition alerts within that invocation. Between invocations the Python process has exited and orchestrator state lives only in the contract file. R4-driven acceptance criterion.
2. **Background-thread lifetime**: the generator returns cleanly (background threads joined) on the normal completion path **and** on `GeneratorExit` (the flattened driver exiting between yields). No leaked threads across the skill→Python boundary.
3. **Contract-state synchronization**: the in-process orchestrator uses the same `.egg-state/contracts/<id>.json` filesystem write path the HTTP daemon uses — no separate state store. The `pending_hitl` envelope is layered onto the contract under a dedicated key.
4. **Existing primitives stay in the path**: `build_system_prompt` (`shared/egg_harness/prompt.py:24`), `ConcurrentPhaseExecutor` (`orchestrator/concurrent_executor.py:114`), `HITLDecision` (`orchestrator/models.py:300`), `PeerConsensusTracker` (`orchestrator/peer_consensus.py:69`).
5. **`EGG_SUBSTRATE=k3s` raises `NotImplementedError`** with a message naming the k3s HTTP daemon entry. k3s users keep using `orchestrator/cli.py:83 cmd_serve`; the *in-process* entry is claude-code-only.

## The `egg-sdlc` plugin

`plugins/egg-sdlc/` is the skill entry point for the claude-code substrate. It is a thin wrapper that drives `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py` in a loop and renders each yielded `HITLDecision` via `AskUserQuestion`.

- `plugins/egg-sdlc/.claude-plugin/plugin.json` carries the `egg.install_instructions` field (reviewer v1 blocker #8: the previous `python_dependency` field was a TODO string the preflight printed verbatim as the install command). Until cq-12 settles on a pip-installable package name, the field holds the actionable from-source command (`git clone … && pip install -r requirements.txt && export PYTHONPATH=…`). `bin/preflight.py` and SKILL.md read from the same field so the install error stays in sync. Resolving cq-12 + publishing a pip name swaps this back to a `python_dependency`-style field; tracked in the rollout.
- `plugins/egg-sdlc/skills/egg-sdlc/SKILL.md` documents the user-facing heredoc-HITL loop, the flattened bridge mechanism, and the slice-by-slice rollout status (refine-team landed in slice 1; plan / implement / pr land in slices 2 / 3 / 4 of #2717).
- `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py` is the flattened stage driver added by slice 1 (TASK-1-1). It imports `run_pipeline_in_process(...)` and round-trips a single `HITLDecision` through `.egg-state/contracts/<id>.json#pending_hitl` per invocation.
- `plugins/egg-sdlc/skills/egg-sdlc/agents/` holds the per-role prompt-prepend files the in-process orchestrator's `build_system_prompt(sources)` reads via the `role_rubric_loader` injected in `select_substrate(...)` (reviewer v1 blocker #5). The files mirror the layout of `plugins/refine-plan/skills/refine-plan/agents/` so the prompt assembler does not need per-skill custom logic. Refine-team rubrics that ship with slice 1 of the #2717 rollout:
  - `agents/refiner.md` — refiner role (landed with the original spike).
  - `agents/reviewer_refine.md` — refine-team reviewer for analysis quality, research depth, options analysis, and open-question specificity (TASK-1-4).
  - `agents/reviewer_agent_design.md` — refine-team reviewer for agent-mode design alignment and anti-patterns; spawned only when the target repo is `jwbron/egg` (TASK-1-4).

The loader at `orchestrator/substrate/__init__.py:232 _load_egg_sdlc_role_rubric` returns the rubric body for any role whose `.md` file is present in `agents/` and raises `ValueError` for roles whose rubric is not yet on the substrate (plan / implement / pr roles until later slices land — TASK-1-6).

## Conformance proof: substrate-parameter CI matrix

cq-3 picked "extend `integration_tests/regression/` with a substrate parameter (CI matrix)". The spike lands the minimum proof; slice 1 of #2717 adds the R2 + flattened-bridge proofs; the full factor-out across all 5 curated issues lands in slice 4.

- A `substrate` parametrize-able fixture in `integration_tests/regression/conftest.py` (values: `"k3s"`, `"claude-code"`). The claude-code dimension `pytest.skip`s when running inside an in-sandbox-agent trust context.
- A substrate-distinguishing test at `integration_tests/regression/test_substrate_smoke.py` parametrized over both substrates. It drives `select_substrate(...).spawner.spawn(...)` (asserts the round-trip returns an `AgentResult` instance) and `.bus.add_message / .bus.get_messages` (asserts INV-3 stale-version rejection round-trip). Both parameters run pure-Python in-process — no kubectl gate. The smoke does not assert populated `AgentResult.commit_sha` because the k3s adapter returns `None` by design (see `AgentSpawner.spawn` docstring); the populated-SHA path is covered by `shared/tests/test_claude_code_spawner.py` for the claude-code leg.
- **Slice 1 of #2717 additions**: `integration_tests/regression/test_bridge_flattened_round_trip.py` (TASK-1-3) asserts the flattened bridge round-trips a `HITLDecision` across two `bin/run_pipeline.py` invocations via the `pending_hitl` envelope. `integration_tests/regression/test_pretooluse_hook_nested.py` (TASK-1-5) asserts the PreToolUse hook denies a child subagent's write when the parent role's allow-list would otherwise permit it, and writes the R2 verdict. `shared/tests/test_rubric_loader.py` (TASK-1-7) asserts `_load_egg_sdlc_role_rubric` returns the new refine-team reviewer rubrics and still raises `ValueError` for plan / implement roles.
- Protocol-conformance unit tests under `shared/tests/` for each implementation (`ClaudeCodeSpawner`, `K3sSpawnerAdapter`, `InProcessMessageBus`, `PreToolUseHookPolicy`, `LocalWorktreeManager`, `run_pipeline_in_process`). The bus tests mirror the scenarios in `orchestrator/tests/test_brc_open_nacks_barrier.py` and `orchestrator/tests/test_brc_content_validation.py` — the behavioral oracle.

## Primitives table

Existing primitives the spike reuses or wraps, and new primitives the spike creates.

### Existing (reused or wrapped)

| Primitive | Module |
|---|---|
| `class KubernetesSpawner`, `def spawn_agent_job` | `orchestrator/kubernetes_spawner.py` |
| `class KubernetesMonitor` | `orchestrator/kubernetes_monitor.py` |
| `def create_concurrent_spawn_fn` | `orchestrator/kubernetes_spawner.py:1564` — wrapped by `K3sSpawnerAdapter` |
| `class MessageStore` (in-memory) | `orchestrator/message_store.py:200` — basis for `InProcessMessageBus` |
| `class RedisMessageStore` | `orchestrator/redis_message_store.py` |
| `class ConcurrentPhaseExecutor`, `def _spawn_agent` | `orchestrator/concurrent_executor.py` — `_spawn_agent` is the patched dispatch seam |
| `def cmd_serve`, `def cmd_pipelines_create` | `orchestrator/cli.py` — `cmd_pipelines_create` is the model for `run_pipeline_in_process` |
| `def validate_invariants` (INV-3, INV-5, INV-6) | `orchestrator/action_guards.py` |
| `class HITLDecision` | `orchestrator/models.py:300` — yielded by `run_pipeline_in_process` |
| `class PeerConsensusTracker` | `orchestrator/peer_consensus.py:69` |
| `def build_system_prompt`, `PromptSource` | `shared/egg_harness/prompt.py` — the structural depth fix #2622 relies on; `ClaudeCodeSpawner` MUST keep it in the path |
| `def set_permission_callback` | `shared/egg_harness/tools/registry.py` |
| `class AgentFilePattern`, `def build_agent_patterns`, `def check_agent_restrictions` | `shared/egg_restrictions/patterns.py`, `gateway/phase_filter.py:1061` — single source of truth shared between gateway and PreToolUse hook |
| `WORKTREE_BASE_DIR`, `is_relative_to` + `resolve()` defense | `gateway/worktree_manager.py:49`, `:1700-1711` (within `list_orphan_worktree_dirs` at `:1687`) |
| Regression-suite fixtures | `integration_tests/regression/conftest.py` |
| `plugins/refine-plan/skills/refine-plan/SKILL.md`, `agents/refiner.md` | reference shape for the new `egg-sdlc` plugin |

### New (created by this spike)

| Primitive | Module |
|---|---|
| `AgentSpawner` (Protocol), `AgentResult` (dataclass with `commit_sha`) | `orchestrator/substrate/spawner.py` |
| `MessageBus` (Protocol) | `orchestrator/substrate/message_bus.py` |
| `PolicyEnforcer` (Protocol) | `orchestrator/substrate/policy.py` |
| `WorktreeManager` (Protocol) | `orchestrator/substrate/worktree.py` |
| `select_substrate(env)` factory + `SubstrateBundle` | `orchestrator/substrate/__init__.py` |
| `K3sSpawnerAdapter` | `orchestrator/substrate/k3s_adapter.py` |
| `ClaudeCodeSpawner` | `orchestrator/substrate/claude_code/spawner.py` |
| `InProcessMessageBus` | `orchestrator/substrate/claude_code/message_bus.py` |
| `PreToolUseHookPolicy`, hook entry script, `settings.template.json` | `orchestrator/substrate/claude_code/policy.py`, `hook_entry.py`, `settings.template.json` |
| `LocalWorktreeManager` | `orchestrator/substrate/claude_code/worktree.py` |
| `run_pipeline_in_process(...)` generator | `orchestrator/substrate/in_process.py` |
| `egg-sdlc` plugin metadata + SKILL.md + per-role rubrics | `plugins/egg-sdlc/.claude-plugin/plugin.json`, `plugins/egg-sdlc/skills/egg-sdlc/SKILL.md`, `plugins/egg-sdlc/skills/egg-sdlc/agents/{refiner,reviewer_refine,reviewer_agent_design}.md` (reviewer rubrics added by slice 1 of #2717, TASK-1-4) |
| `bin/run_pipeline.py` flattened stage driver (slice 1, TASK-1-1) | `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py` |
| Test-only nested-Agent-tool dispatch fake (slice 1, TASK-1-9) | `integration_tests/regression/_agent_tool_fake.py` |
| R2 nested-dispatch test (slice 1, TASK-1-5) | `integration_tests/regression/test_pretooluse_hook_nested.py` |
| `substrate` pytest fixture + parametrized smoke test | `integration_tests/regression/conftest.py`, `integration_tests/regression/test_substrate_smoke.py` |
| ADR + rollout-deltas tracker | `docs/architecture/claude-code-substrate.md` (this file) |

## What's NOT the same as before (risk-mitigation subsections)

The risk_analyst identified several risks that materially shift egg's behavior under the new substrate. Each is documented here per **REC2** so the ADR is more than "how the substrate works" — it is also the audit-trail for *what changes* and the operator's explicit acceptance of those changes.

### Trust-context shift (R1)

**The change.** Today the Anthropic API key is gateway-isolated: the sandbox NEVER sees the real key — the gateway intercepts API requests and injects credentials server-side (`gateway/anthropic_credentials.py`); the sandbox sees only a `sk-ant-oat01-PROXY-INJECTED-...` placeholder. In the Claude Code substrate, the parent Claude session HOLDS the real key, and every subagent the orchestrator spawns inherits the session's credential context. A subagent compromised via prompt injection (untrusted issue body, malicious PR content) can in principle read or exfiltrate the key from environment / disk / network; in the k3s model the same compromise would only have access to the placeholder.

**Why it's accepted.** The substrate-swap goal is to run egg from a single developer's Claude Code session against the developer's own repos. The threat model is not "agent from a randomly-encountered issue" — it is the user's own SDLC. Keeping the gateway in the loop contradicts the substrate-swap goal; documenting the shift and scoping use to trusted repos is the realistic mitigation (R1 mitigation strategy).

**Operator acceptance.** Per the cq-1 = "parallel substrates" selection, the operator explicitly accepts running the claude-code substrate against *trusted-repo* SDLC streams. The gateway-isolated k3s substrate remains available indefinitely (cq-9) for any caller that needs the credential-isolation boundary.

**Mitigations in this spike.**

- Skill imports never log credentials. The plugin's pre-flight import does not echo env to stdout.
- The PreToolUse hook entry script does not exfiltrate environment to stdout; it only emits `deny` / `allow` + `message`.
- The install docs (SKILL.md) name the trust-context shift explicitly so the user reads it before installing.
- Future work: an opt-in `credentialed-proxy` mode that routes the claude-code substrate through a local gateway-equivalent for users running against untrusted issue streams. Tracked in the follow-up.

### PreToolUse hook fallback (R2)

**The primary seam.** cq-6 selected PreToolUse hooks as the policy enforcement boundary. The hook reads tool name + tool input from stdin (PreToolUse contract), imports `build_agent_patterns` from `shared/egg_restrictions/patterns.py:768`, and emits `deny` + `message` JSON to stdout when the write target is outside the caller's role's allow-list.

**The empirical question.** Whether Claude Code's PreToolUse hooks can reliably resolve "which subagent / role is calling Write()" from the hook's process context under nested Agent-tool dispatch. The spike validated the single-role path; **slice 1 of #2717 ships the worked 2-subagent example** as the cq-5 early-spike gating test:

- `integration_tests/regression/test_pretooluse_hook_nested.py` (TASK-1-5) spawns a parent fake-subagent with `EGG_AGENT_ROLE=architect` and a nested child fake-subagent with `EGG_AGENT_ROLE=tester`. It asserts the hook denies a write to `orchestrator/foo.py` from the child even though the parent's role would allow it.
- `integration_tests/regression/_agent_tool_fake.py` (TASK-1-9) is the test-only Agent-tool dispatch fake; it simulates Claude Code's `Agent` tool by spawning a subprocess with controlled `EGG_AGENT_ROLE` per dispatch and invokes `hook_entry.decide(...)` via each fake's `pre_tool_use_callback`. **Test infrastructure only** — not registered in `select_substrate`, not a production spawner.
- The test writes the verdict to `.egg-state/<pipeline_id>/r2-verdict.json` as either `{"r2_verdict": "pass"}` or `{"r2_verdict": "fail", "reason": "..."}`. Slice 5's contingent R15 migration task reads this file.

**What R2 today validates (and what it does not).** Production dispatch under cq-3 remains on `ClaudeCodeSpawner` (the harness re-host model) — `shared/egg_harness/client.py:60-150` uses its own `ToolRegistry.set_permission_callback(...)` and does NOT invoke the PreToolUse hook. R2 therefore validates hook *logic* given accurate `EGG_AGENT_ROLE` propagation; it does **not** validate that Claude Code itself propagates `EGG_AGENT_ROLE` correctly under real nested Agent-tool dispatch (which is verifiable only by running real Claude Code, which the in-sandbox test cannot do). The R2 result becomes load-bearing only if cq-3 flips to Agent-tool dispatch in a future issue. The test docstring documents this limitation.

**Documented fallback path.** If the R2 verdict is `fail`, slice 5 wires the fallback — **cq-6 option 2 — MCP-validator-side enforcement** combined with R15 model (b) migration: agent-side enforcement at `sandbox/egg_agent_tools/handlers/restrictions.py` re-validates the caller's role + path against `patterns.py`, and every role rubric moves to a real `.claude/agents/<role>.md` definition with frontmatter tool restrictions. This fallback is known to work because egg already ships `check_file_restriction` as an MCP tool today.

### Subagent context budget regression (R7)

**The change.** Egg targets `max_turns: 1000` in concurrent execution today (`docs/guides/concurrent-execution.md:97`). Claude Code subagents inherit a smaller context budget from the model's hard limit. Deep refines of large issues may exhaust the subagent context before they finish.

**What the spike accepts.** cq-10 picked hybrid checkpoint + fork. **The spike implements the lighter half: checkpoints.** Forking a subagent for sub-task delegation is documented but deferred to the follow-up — it is a quality booster, not a correctness requirement. The follow-up issue captures fork-based sub-task delegation explicitly.

**The fallback if checkpoints prove inadequate.** cq-10 option 2 (accept smaller-than-1000 turn budget; role rubrics and system-prompt depth do most of the work; deep-research breadth is reduced but consistent) is the documented escape path. The spike does not commit to "hybrid works for all role workloads"; it commits to "checkpoints are in the path, the smaller-budget fallback is named, and the follow-up will measure".

### Interface stability marker (R10)

The four `Protocol`s in `orchestrator/substrate/` carry a `# v0.x — unstable until ≥3 roles exercise` comment in their module docstrings. The ADR states the interfaces are **explicitly subject to revision** after the follow-up issue extends the substrate to plan / implement / pr roles. Downstream code should treat the interfaces as a moving target until at least three roles run through them end-to-end.

The risk this manages: a single-role spike does not exercise the interface diversity the second wave needs. The plan's design reviewer reasoned through the interfaces against the full role roster, but design review is not a substitute for end-to-end exercise. Marking the interfaces unstable lets the follow-up iterate without breaking-change ceremony.

### Cost cap recommendation (REC5)

**The change.** Today the gateway can rate-limit and per-pipeline cap Anthropic API usage server-side. In the Claude Code substrate, every agent invocation bills directly to the user's Anthropic account / OAuth token. A buggy infinite-loop in a single phase (e.g., a NACK ping-pong) could rack up significant cost before the user notices.

**Recommended mitigation.** An `EGG_PIPELINE_MAX_AGENT_INVOCATIONS` env var with a conservative default (e.g., 50) — a hard cap on total agent dispatches per pipeline run, configurable upward for legitimate large slice-DAGs.

**Spike scope.** This spike **does not implement** the cost cap. The recommendation is captured here as ADR-level guidance for the follow-up. The mechanically simple implementation (counter + check + raise) is intentionally deferred to keep the spike scope tight — the follow-up issue lists `EGG_PIPELINE_MAX_AGENT_INVOCATIONS` as one of the deferred items.

### Subagent type model (R15)

Claude Code supports two subagent-dispatch models:

- **Model (a)**: `Agent` tool with `subagent_type="general-purpose"` plus an ad-hoc prompt assembled by the spawner. Tool restrictions rely on PreToolUse hooks + prompt discipline.
- **Model (b)**: `Agent` tool with `subagent_type="<custom-agent-name>"` resolving to a `.claude/agents/<name>.md` file with frontmatter (tool restrictions, model, allowed bash commands). Structural enforcement of tool restrictions per role.

**The spike (and slice 1 of #2717) picks model (a)** — `subagent_type="general-purpose"` — to match the existing `plugins/refine-plan/skills/refine-plan/SKILL.md` layout. The refine-team role files at `plugins/egg-sdlc/skills/egg-sdlc/agents/{refiner,reviewer_refine,reviewer_agent_design}.md` are prepended to the assembled prompt by the in-process orchestrator's `build_system_prompt(sources)`.

**Trade-off the ADR records.** Model (a) is simpler to ship (no `.claude/agents/<role>.md` generator needed yet) but pushes tool-restriction enforcement entirely onto the PreToolUse hook (R2) plus prompt discipline. Model (b) gives structural tool restrictions per role but requires building (or vendoring) per-role agent definition files at skill install time. **The migration from (a) to (b) is contingent on the slice-1 R2 verdict** (`.egg-state/<pipeline_id>/r2-verdict.json`): if R2 passes, slice 5 of the #2717 rollout keeps every role on model (a); if R2 fails, slice 5 migrates every rubric to a real `.claude/agents/<role>.md` definition and adds agent-side enforcement at `sandbox/egg_agent_tools/handlers/restrictions.py`.

## Trust-context note (existing doc cross-reference)

The integration-test trust-boundary doc (`docs/architecture/integration-test-trust-boundary.md`) distinguishes execution contexts: in-sandbox-agent / trusted-CI-runner / human-operator. The substrate swap shifts most agent execution from "in-sandbox" to "in-parent-Claude-Code-session" — that's a new trust context.

For the spike, the substrate-parameter regression fixture at `integration_tests/regression/conftest.py` `pytest.skip`s the claude-code dimension when running inside an in-sandbox-agent trust context (detected via the existing env-var heuristic). Both substrate parameters otherwise run pure-Python in-process and do not depend on `egg_stack` / `orchestrator_url` fixtures — no kubectl gate is needed for either dimension.

The trust-boundary doc itself is not edited by this spike or slice 1 of #2717; the new context is named here for forward reference. The rollout may elevate "in-parent-Claude-Code-session" to a first-class entry in the trust-boundary doc as part of slice 5 hardening.

## Rollout deltas

One row per item the original spike (#2623) deferred. Status is updated as each slice of the #2717 rollout lands. The acceptance bar for the rollout is "every row in the **Completed in this rollout** subsection, and every interface tags shift to stable" (see [Acceptance / definition of done](#acceptance--definition-of-done) below).

### Completed in this rollout

- [x] **Close the heredoc-HITL bridge gap for refine + plan phases (slice 1).** ~~The spike's `run_pipeline_in_process(...)` generator yielded `HITLDecision` objects without a shipped driver that could ferry them to `AskUserQuestion` and back.~~ Slice 1 lands `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py` (TASK-1-1) — a flattened single-yield stage driver per cq-1 = hybrid (Option C). Each invocation round-trips one `HITLDecision` through `.egg-state/contracts/<id>.json#pending_hitl`. The skill loops over invocations, rendering each decision via `AskUserQuestion` and writing the operator's selection back to the contract. Slice 3 ships the daemon variant for implement-phase concurrency; both variants consume the same `pending_hitl` envelope shape (risk_analyst R17 mitigation).
- [x] **Refine-team expansion (slice 1).** ~~The spike ran the refiner role alone; `reviewer_refine` and `reviewer_agent_design` were k3s-only.~~ Slice 1 adds `plugins/egg-sdlc/skills/egg-sdlc/agents/reviewer_refine.md` and `agents/reviewer_agent_design.md` (TASK-1-4) so the refine phase now exercises the full refine-team roster on the substrate. The loader at `orchestrator/substrate/__init__.py:232 _load_egg_sdlc_role_rubric` (TASK-1-6) returns the rubric body for the two new reviewers; plan / implement / pr roles still raise `ValueError` with a pointer to the next slice.
- [x] **R2 empirical-question 2-subagent worked example (slice 1).** Slice 1 lands `integration_tests/regression/test_pretooluse_hook_nested.py` (TASK-1-5) + `integration_tests/regression/_agent_tool_fake.py` (TASK-1-9) as the cq-5 early-spike gating test. The test drives the PreToolUse hook through a parent → child Agent-tool dispatch via the test-only fake and writes the verdict to `.egg-state/<pipeline_id>/r2-verdict.json`. See [PreToolUse hook fallback (R2)](#pretooluse-hook-fallback-r2) for the load-bearing-only-when-cq-3-flips qualifier and the slice-5 fallback.

### Pending in this rollout

- [ ] **Extend claude-code substrate to plan / implement / pr phases (cq-2 unfinished).** `run_pipeline_in_process(...)` is refine-only today and raises `NotImplementedError` for the other phases. Slice 2 wires the plan phase's role roster (architect / task_planner / risk_analyst + reviewer_plan) and exercises BRC consensus end-to-end on the new substrate. Slice 3 lands implement-phase substrate + daemon HITL bridge. Slice 4 lands pr-phase substrate.
- [ ] **Full conformance matrix across all 5 curated issues (feedback Q1).** Slice 4 wires the substrate-parameter on every `integration_tests/regression/` test that is substrate-portable; classifies each test as portable / k3s-only / claude-code-only.
- [ ] **Set perf / latency budget (feedback Q2).** Slice 5 sets a measured ratio (e.g., "refine phase ≤ Nx k3s latency") and a `pytest.mark.slow` gate for any test that exceeds it. Numbers feed back into the ADR.
- [ ] **Implement the full k3s interface adapter (cq-1 k3s side).** Slice 4 / 5 promote `RedisMessageStore`, the gateway-equivalent policy enforcer, and `gateway/worktree_manager.py` onto the `MessageBus`, `PolicyEnforcer`, and `WorktreeManager` protocols. Removes the cq-11 scope-fence that gates `EGG_SUBSTRATE=k3s` on the legacy seam.
- [ ] **Optional `EggHarnessSpawner` (feedback Q4).** Slice 5. Subprocess-driven `egg_harness` spawner for headless / CLI mode (`egg-orch local-run --issue 1234`).
- [ ] **Ship `egg-state prune` CLI verb (feedback Q6).** Beyond #2717. Local checkpoint and worktree cleanup verb so users running the claude-code substrate can prune `.egg-state/<pipeline_id>/` after pipeline completion.
- [ ] **Fork-based sub-task delegation (cq-10 deferred half).** Slice 5. A refiner whose context fills up forks a child subagent to do a sub-task; the child's summary returns to the parent.
- [ ] **Implement `EGG_PIPELINE_MAX_AGENT_INVOCATIONS` cost cap (REC5).** Slice 5. Pipeline-level cap on total agent dispatches, with a conservative default (e.g., 50). Per-phase cost reporting in the parent session.
- [ ] **Migrate to custom `subagent_type` per-role agent files (R15 model (b)) — contingent on R2.** Slice 5, contingent. If the slice-1 R2 verdict (`.egg-state/<pipeline_id>/r2-verdict.json`) is `pass`, the substrate stays on model (a). If `fail`, slice 5 migrates every role rubric under `plugins/egg-sdlc/skills/egg-sdlc/agents/` to a real `.claude/agents/<role>.md` definition with frontmatter tool restrictions AND adds agent-side enforcement at `sandbox/egg_agent_tools/handlers/restrictions.py` (cq-6 option 2).
- [ ] **Stabilize the four substrate interfaces.** Slice 5. Drop the `# v0.x — unstable until ≥3 roles exercise` marker (R10) once the rolled-out phases have run at least three roles through each interface end-to-end. Document explicit interface-stability criteria.

## Acceptance / definition of done

- The Claude Code substrate runs refine + plan + implement + pr against the curated 5 issues. The conformance matrix passes on both substrate dimensions for every substrate-portable test.
- A measured perf / latency budget is in this ADR; a `pytest.mark.slow` gate enforces it.
- The four substrate interfaces have lost their `unstable` marker.
- `EGG_PIPELINE_MAX_AGENT_INVOCATIONS`, `egg-state prune`, and at least one of (`EggHarnessSpawner`, fork-based sub-task delegation) ship as documented.
