# Claude Code Substrate (walking-skeleton spike for #2623)

> Status: **walking skeleton / unstable** — the four substrate `Protocol`s land in this PR with one role (refiner) exercising them end-to-end on the Claude Code side and a thin `K3sSpawnerAdapter` shim keeping `EGG_SUBSTRATE=k3s` green. The interfaces are `# v0.x — unstable until ≥3 roles exercise` (R10). Plan / implement / pr phases, the broader role roster, the full conformance matrix, the real k3s interface adapter, and the cost-cap / prune / fork primitives are tracked in the [Follow-up issue draft](#follow-up-issue-draft-reviewer-pasted-not-auto-filed).

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
| **cq-2** parent-close phase scope | All phases (refine + plan + implement + pr) | **Spike scope is refine-only** per cq-11. The follow-up extends to plan / implement / pr |
| **cq-3** conformance scoping | Extend `integration_tests/regression/` with a `substrate` parameter (CI matrix) | One regression test parametrized via the new fixture in this spike; full matrix factor-out deferred |
| **cq-4** spawner shape | Synchronous `spawn(role, prompt, env, worktree) → AgentResult` | `AgentSpawner` protocol pinned at this signature |
| **cq-5** worktree ownership | Port `WORKTREE_BASE_DIR` model | `LocalWorktreeManager` mirrors `gateway/worktree_manager.py:49` shape; per-agent worktrees land at `<base>/<pipeline_id>/<role>/`; default `<base>` is `~/.egg-worktrees/`, `EGG_WORKTREE_BASE` overrides (typical override: `./.egg-state/`) |
| **cq-6** policy seam | PreToolUse hooks | `PreToolUseHookPolicy` ships a hook entry script + `settings.template.json`; calls the existing `shared/egg_restrictions/patterns.py:768 build_agent_patterns` |
| **cq-7** HITL surface | Heredoc-style synchronous generator | `run_pipeline_in_process(...)` is a generator yielding `HITLDecision` objects; the skill renders each via `AskUserQuestion` and resumes via `.send(...)`. **Walking-skeleton gap:** the multi-yield generator↔`AskUserQuestion` bridge from a Bash-spawned `python3` subprocess is unsolved in the spike and ships in the follow-up — see "Bridge gap" callout in the in-process orchestrator section. |
| **cq-8** packaging | Plugin metadata declares pip dependencies | `plugins/egg-sdlc/.claude-plugin/plugin.json` declares the pip dep selected by cq-12 |
| **cq-9** k3s disposition | Leave indefinitely | k3s code untouched in this spike; deprecation is a future-issue question |
| **cq-10** context-window strategy | Hybrid — checkpoint + fork | Spike ports the checkpoint half; forking is deferred to follow-up (it's a quality booster, not a correctness requirement) |
| **cq-11** slice shape | Spike then plan | One slice; follow-up issue captured in this ADR's appendix |
| **cq-12** canonical pip name | (resolved in plan re-propose cycle) | `plugins/egg-sdlc/.claude-plugin/plugin.json` carries the operator's selection verbatim |

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

- **k3s implementation**: `K3sSpawnerAdapter` (in `orchestrator/substrate/k3s_adapter.py`) wraps `create_concurrent_spawn_fn` (`orchestrator/kubernetes_spawner.py:1564`). It returns `AgentResult.commit_sha=None` by design: the legacy factory is fire-and-monitor, so a `git rev-parse HEAD` on the orchestrator host at adapter-return time would capture the *pre*-spawn HEAD and BRC reviewers would attach commit-bound ACKs to the wrong SHA. The legitimate INV-6 SHA for k3s is supplied through the existing gateway-side attestation channel that reads it off `SpawnedContainer.container_info` after the pod terminates. Plumbing that channel into the protocol's `commit_sha` field directly is tracked in the [Follow-up issue draft](#follow-up-issue-draft-reviewer-pasted-not-auto-filed) ("wire gateway attestation into `AgentResult.commit_sha`"). No behavior change for k3s users — the dispatch seam at `_spawn_agent` is gated on `EGG_SUBSTRATE=claude-code` only and the legacy path remains in place for unset / `"k3s"` (reviewer v1 blocker #1).
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

The walking-skeleton's most expensive task. Today the orchestrator is a Flask + waitress HTTP daemon (`orchestrator/cli.py:83 cmd_serve`) with `ConcurrentPhaseExecutor` (`orchestrator/concurrent_executor.py:114`) running its own `ThreadPoolExecutor` and `PeerConsensusTracker` (`orchestrator/peer_consensus.py:69`) holding its own locks. There is **no in-process / embedded / local mode** today — `egg-orch` is a thin HTTP client.

`run_pipeline_in_process(...)` (in `orchestrator/substrate/in_process.py`) is a Python generator that yields `HITLDecision` objects (`orchestrator/models.py:300`) when the pipeline pauses for a human decision. The intended cq-7 surface is "skill renders each via `AskUserQuestion`, sends the answer back via `generator.send(...)`, and the orchestrator resumes". This is **cq-7 = heredoc-style synchronous** — a hybrid of the parent-session `AskUserQuestion` and filesystem-journal options. **Driver / bridge deferred** — see the callout below.

> **Bridge gap, reviewer v1 blocker #6 + v2 blocker B1 (deferred to the follow-up).** A Claude Code skill cannot drive a long-lived Python generator across multiple `AskUserQuestion` round-trips today — `AskUserQuestion` is a tool the LLM calls, not a function callable from a `python3` subprocess, and every `python3` invocation from a Bash skill step is a fresh process whose `gi_frame` dies at exit. The spike ships the generator (engineered for resumption across yields) and the in-process orchestrator (heartbeat threads, contract-state sync, `GeneratorExit` discipline) **but not the bridge from generator-yield to `AskUserQuestion`-render-and-resume, and not a single-pass `bin/` driver either**. Within a long-lived Python process the generator-side machinery is correct and unit-tested; from a Claude Code skill step, no shipped code today invokes `run_pipeline_in_process(...)`. (The earlier v1 docs mentioned a `--preflight-answer` CLI fallback — reviewer v2 caught that no such flag, env var, or driver script was ever shipped; that claim was a docs-vs-code drift and is removed.) Two design options the follow-up will pick between: (a) a long-lived Python REPL/daemon the skill talks to via JSON-RPC; (b) flatten the generator into a hand-shaped sequence of single-yield `python3 <stage>.py` invocations whose decisions and answers thread through the contract file. The follow-up issue's first bullet reserves this gap.

Key properties:

1. **Heartbeat-during-HITL**: while the generator is paused at a yield boundary, the in-process orchestrator's background threads (heartbeat poll, BRC re-review, message-bus tick) continue to run so a long-paused HITL does not cause stuck-phase-transition alerts. R4-driven acceptance criterion.
2. **Background-thread lifetime**: the generator returns cleanly (background threads joined) on the normal completion path **and** on `GeneratorExit` (if the caller drops the generator without exhausting it). No leaked threads on mid-cycle abort.
3. **Contract-state synchronization**: the in-process orchestrator uses the same `.egg-state/contracts/<id>.json` filesystem write path the HTTP daemon uses — no separate state store. Reading the contract file after the generator yields its first `HITLDecision` shows the pending-decision entry exactly as the HTTP daemon would write it.
4. **Existing primitives stay in the path**: `build_system_prompt` (`shared/egg_harness/prompt.py:24`), `ConcurrentPhaseExecutor` (`orchestrator/concurrent_executor.py:114`), `HITLDecision` (`orchestrator/models.py:300`), `PeerConsensusTracker` (`orchestrator/peer_consensus.py:69`).
5. **`EGG_SUBSTRATE=k3s` raises `NotImplementedError`** with a message naming the follow-up issue. k3s users keep using the HTTP daemon entry (`orchestrator/cli.py:83 cmd_serve`); the *in-process* entry is claude-code-only in this spike — the explicit cq-11 scope-fence.

## The `egg-sdlc` plugin

`plugins/egg-sdlc/` is the skill entry point for the claude-code substrate. **In the target shape** it is a thin wrapper that imports `run_pipeline_in_process`, drives the generator, and renders each yielded `HITLDecision` via `AskUserQuestion`. **What ships in this spike** is the install / pre-flight surface plus the per-role rubric / docs; the orchestrator-boot driver and the `AskUserQuestion` bridge are deferred per the bridge-gap callout above.

- `plugins/egg-sdlc/.claude-plugin/plugin.json` carries the `egg.install_instructions` field (reviewer v1 blocker #8: the previous `python_dependency` field was a TODO string the preflight printed verbatim as the install command). Until cq-12 settles on a pip-installable package name, the field holds the actionable from-source command (`git clone … && pip install -r requirements.txt && export PYTHONPATH=…`). `bin/preflight.py` and SKILL.md read from the same field so the install error stays in sync. Resolving cq-12 + publishing a pip name swaps this back to a `python_dependency`-style field; tracked in the follow-up.
- `plugins/egg-sdlc/skills/egg-sdlc/SKILL.md` documents the user-facing heredoc-HITL loop as the **target** shape, explicitly marks the orchestrator-driver / bridge gap as deferred, and states the eventual exercised scope is refiner-only.
- `plugins/egg-sdlc/skills/egg-sdlc/agents/refiner.md` is the per-role prompt-prepend file the in-process orchestrator's `build_system_prompt(sources)` reads via the `role_rubric_loader` injected in `select_substrate(...)` (reviewer v1 blocker #5). It mirrors the layout of `plugins/refine-plan/skills/refine-plan/agents/refiner.md` so the prompt assembler doesn't need per-skill custom logic.

## Conformance proof: substrate-parameter CI matrix

cq-3 picked "extend `integration_tests/regression/` with a substrate parameter (CI matrix)". The spike lands the minimum proof — full factor-out is deferred:

- A `substrate` parametrize-able fixture in `integration_tests/regression/conftest.py` (values: `"k3s"`, `"claude-code"`). The claude-code dimension `pytest.skip`s when running inside an in-sandbox-agent trust context.
- A new substrate-distinguishing test at `integration_tests/regression/test_substrate_smoke.py` parametrized over both substrates. It drives `select_substrate(...).spawner.spawn(...)` (asserts the round-trip returns an `AgentResult` instance) and `.bus.add_message / .bus.get_messages` (asserts INV-3 stale-version rejection round-trip). Both parameters run pure-Python in-process — no kubectl gate. The smoke does not assert populated `AgentResult.commit_sha` because the k3s adapter returns `None` by design (see `AgentSpawner.spawn` docstring); the populated-SHA path is covered by `shared/tests/test_claude_code_spawner.py` for the claude-code leg.
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
| `egg-sdlc` plugin metadata + SKILL.md + `agents/refiner.md` | `plugins/egg-sdlc/.claude-plugin/plugin.json`, `plugins/egg-sdlc/skills/egg-sdlc/SKILL.md`, `plugins/egg-sdlc/skills/egg-sdlc/agents/refiner.md` |
| `substrate` pytest fixture + parametrized smoke test | `integration_tests/regression/conftest.py`, `integration_tests/regression/test_substrate_smoke.py` |
| ADR + follow-up issue draft | `docs/architecture/claude-code-substrate.md` (this file) |

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

**The open question.** Whether Claude Code's PreToolUse hooks can reliably resolve "which subagent / role is calling Write()" from the hook's process context is **not yet established empirically**. The spike ships the hook against the single-role refiner case — `EGG_AGENT_ROLE` is set in the spawn env and the hook reads it. That validates the single-subagent path but does NOT validate role-routing under nested / multi-subagent dispatch, which the spike does not exercise (refiner-only scope per cq-11). **Ownership of the multi-role validation belongs to the follow-up**, not the spike — see [Follow-up issue draft, "Validate PreToolUse hook role-routing (R2 empirical question)"](#follow-up-issue-draft-reviewer-pasted-not-auto-filed). The spike merges with the hook in place and the single-role evidence; the follow-up issue is where the worked 2-subagent example lives.

**Documented fallback path.** If the follow-up's evidence shows the PreToolUse hook cannot reliably resolve the caller's role for nested subagent dispatch, the fallback is **cq-6 option 2 — MCP-validator-side enforcement**: every state-mutating MCP verb re-validates the caller's role + path against `patterns.py`. This fallback is known to work because egg already ships `check_file_restriction` as an MCP tool today.

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

**The spike picks model (a)** — `subagent_type="general-purpose"` — to match the existing `plugins/refine-plan/skills/refine-plan/SKILL.md` layout. The refiner role file at `plugins/egg-sdlc/skills/egg-sdlc/agents/refiner.md` is prepended to the assembled prompt by the in-process orchestrator's `build_system_prompt(sources)`.

**Trade-off the ADR records.** Model (a) is simpler to ship (no `.claude/agents/<role>.md` generator needed yet) but pushes tool-restriction enforcement entirely onto the PreToolUse hook (R2) plus prompt discipline. Model (b) gives structural tool restrictions per role but requires building (or vendoring) per-role agent definition files at skill install time. The migration from (a) to (b) is reserved for the follow-up — it is a quality-boosting refactor, not a correctness gap, once the PreToolUse hook (R2) is empirically validated.

## Trust-context note (existing doc cross-reference)

The integration-test trust-boundary doc (`docs/architecture/integration-test-trust-boundary.md`) distinguishes execution contexts: in-sandbox-agent / trusted-CI-runner / human-operator. The substrate swap shifts most agent execution from "in-sandbox" to "in-parent-Claude-Code-session" — that's a new trust context.

For the spike, the substrate-parameter regression fixture at `integration_tests/regression/conftest.py` `pytest.skip`s the claude-code dimension when running inside an in-sandbox-agent trust context (detected via the existing env-var heuristic). Both substrate parameters otherwise run pure-Python in-process and do not depend on `egg_stack` / `orchestrator_url` fixtures — no kubectl gate is needed for either dimension.

The trust-boundary doc itself is not edited by this spike; the new context is named here for forward reference. The follow-up issue may want to elevate "in-parent-Claude-Code-session" to a first-class entry in the trust-boundary doc.

## Open work (what the spike does NOT do)

The spike is intentionally narrow. The following are explicitly out of scope and captured in the [Follow-up issue draft](#follow-up-issue-draft-reviewer-pasted-not-auto-filed) below:

- **Other phases**: plan, implement, pr remain k3s-only in the in-process orchestrator. cq-2 picked all phases for the parent-close criterion; cq-11 narrowed the spike to refine-only.
- **BRC concurrency end-to-end**: the spike runs the refiner role alone. The orchestrator-as-bus model is already in place via `InProcessMessageBus`, but the full BRC mechanics (multi-producer, multi-reviewer, ACK / NACK / RE_REVIEW / CONFIRMED cycle) are not exercised by a single-role spike.
- **Full 5-issue conformance**: feedback Q1 settled the curated 5-issue set; the spike runs against ONE of them. The follow-up wires the matrix on all 5.
- **Real k3s interface adapter beyond the shim**: `K3sSpawnerAdapter` works; full `K3sMessageBus`, `K3sPolicyEnforcer`, `K3sWorktreeManager` adapters do not exist yet. The k3s side keeps using its existing concrete implementations; promoting them to satisfy the new protocols is follow-up work.
- **`EggHarnessSpawner`**: feedback Q4 named it as a secondary goal; the spike does not build it.
- **`egg-state prune` verb**: feedback Q6 reserved a CLI verb for local checkpoint cleanup; the spike does not ship it.
- **Fork-based sub-task delegation**: cq-10's hybrid choice has a deferred half (forking subagents for sub-task delegation when context fills); the spike ports only the checkpoint half.
- **`EGG_PIPELINE_MAX_AGENT_INVOCATIONS` implementation**: REC5's cost cap is recommended in this ADR but not implemented in the spike.
- **Custom `subagent_type` migration**: R15's model (b) — per-role agent definitions in `.claude/agents/<role>.md` with structural tool restrictions — is named as a future refactor; the spike uses model (a).

---

## Follow-up issue draft (reviewer-pasted, not auto-filed)

**This section is reviewer-pasted, not auto-filed from the pipeline.** Documenter is role-blocked from `.github/` and cannot create issues. The reviewer who merges the spike PR copies the text below into a new GitHub issue (suggested title: `Roll out the Claude Code substrate from the walking-skeleton spike (follow-up to #2623)`). Edit / re-order freely; this is a starting body, not a contract.

---

## Why this issue exists

The walking-skeleton spike for [#2623](https://github.com/jwbron/egg/issues/2623) landed the four substrate `Protocol`s (`AgentSpawner`, `MessageBus`, `PolicyEnforcer`, `WorktreeManager`), a working Claude Code implementation of each, the in-process orchestrator boot path (`run_pipeline_in_process`), the `egg-sdlc` plugin entry point, one parametrized regression test, and the ADR at [`docs/architecture/claude-code-substrate.md`](../architecture/claude-code-substrate.md). Per **cq-11 = "Spike then plan"**, the spike's job was to prove the spawner shape on one role end-to-end; this follow-up rolls the substrate out from there.

## Rollout deltas

One bullet per item the spike defers. The ADR's "Open work" section names each; this issue is the executable form.

- [ ] **Extend claude-code substrate to plan / implement / pr phases (cq-2 unfinished).** `run_pipeline_in_process(...)` is refine-only today and raises `NotImplementedError` for the other phases. Wire each phase's role roster through the in-process orchestrator and exercise BRC consensus end-to-end on the new substrate.
- [ ] **Close the heredoc-HITL bridge gap (reviewer v1 blocker #6).** The spike's `run_pipeline_in_process(...)` generator yields `HITLDecision` objects and the in-process machinery (heartbeat threads, contract-state sync, `GeneratorExit` discipline) is correct within a single-pass invocation, but the bridge from "Python generator yields a decision" to "skill renders via `AskUserQuestion` and resumes the generator" does not exist — a Bash-spawned `python3` subprocess dies between yields. Pick one of: (a) long-lived Python REPL/daemon the skill talks to via JSON-RPC; (b) flatten the generator into a hand-shaped sequence of single-yield `python3 <stage>.py` invocations the skill orchestrates, with decisions and answers threaded through `.egg-state/contracts/<id>.json`. Ship the chosen approach end-to-end with a multi-yield acceptance test.
- [ ] **Full conformance matrix across all 5 curated issues (feedback Q1).** The spike runs against ONE curated issue; the conformance set is a fixed curated 5 covering SDLC hot paths (one bug fix, one feature add, one refactor, one infra/script change, one doc change). Land the substrate-parameter on every `integration_tests/regression/` test that is substrate-portable; classify each test as portable / k3s-only / claude-code-only.
- [ ] **Set perf / latency budget (feedback Q2).** Feedback Q2 explicitly deferred the budget until real numbers exist. After the rollout completes refine + plan + implement + pr on the curated 5, set a measured ratio (e.g., "refine phase ≤ Nx k3s latency") and a `pytest.mark.slow` gate for any test that exceeds it.
- [ ] **Implement the full k3s interface adapter (cq-1 k3s side).** The spike ships only `K3sSpawnerAdapter`. Promote `RedisMessageStore`, the gateway-equivalent policy enforcer, and `gateway/worktree_manager.py` onto the `MessageBus`, `PolicyEnforcer`, and `WorktreeManager` protocols. This is the cq-1 "parallel substrates" promise the spike deferred per cq-11.
- [ ] **Optional `EggHarnessSpawner` (feedback Q4).** A subprocess-driven `egg_harness` spawner for headless / CLI mode (`egg-orch local-run --issue 1234`) that uses the in-process orchestrator with a non-Claude-Code agent harness. Battle-tests the abstraction with three implementations; unlocks CI usage of the in-process orchestrator without Claude Code.
- [ ] **Ship `egg-state prune` CLI verb (feedback Q6).** Local checkpoint and worktree cleanup verb so users running the claude-code substrate can prune `.egg-state/<pipeline_id>/` after pipeline completion. No telemetry; local-only.
- [ ] **Fork-based sub-task delegation (cq-10 deferred half).** The spike ports only the checkpoint half of cq-10's hybrid. Add the fork primitive: a refiner whose context fills up forks a child subagent to do a sub-task ('read all files matching X and summarize'); the child's summary returns to the parent. Mirrors how a human delegates.
- [ ] **Implement `EGG_PIPELINE_MAX_AGENT_INVOCATIONS` cost cap (REC5).** Pipeline-level cap on total agent dispatches, with a conservative default (e.g., 50). Prevents runaway-cost scenarios (R9) at near-zero implementation cost. Per-phase cost reporting in the parent session so the user sees cost as it accrues.
- [ ] **Migrate to custom `subagent_type` per-role agent files (R15).** Convert each per-role prompt-prepend file under `plugins/egg-sdlc/skills/egg-sdlc/agents/` from "ad-hoc prompt prepended to `subagent_type="general-purpose"`" (model (a)) to a real `.claude/agents/<role>.md` definition with frontmatter (tool restrictions, model, allowed bash commands) (model (b)). Adds structural tool-restriction enforcement on top of the PreToolUse hook layer.
- [ ] **Validate PreToolUse hook role-routing (R2 empirical question).** Produce a worked 2-subagent example demonstrating the hook correctly resolves the calling role for each subagent's tool call. If the hook cannot do this, fall back to MCP-validator-side enforcement (cq-6 option 2) as the policy seam.
- [ ] **Stabilize the four substrate interfaces.** Drop the `# v0.x — unstable until ≥3 roles exercise` marker (R10) once at least three roles have run through them end-to-end across the rolled-out phases. Document explicit interface-stability criteria.

## Acceptance / definition of done

- The Claude Code substrate runs refine + plan + implement + pr against the curated 5 issues. The conformance matrix passes on both substrate dimensions for every substrate-portable test.
- A measured perf / latency budget is in the ADR; a `pytest.mark.slow` gate enforces it.
- The four substrate interfaces have lost their `unstable` marker.
- `EGG_PIPELINE_MAX_AGENT_INVOCATIONS`, `egg-state prune`, and at least one of (`EggHarnessSpawner`, fork-based sub-task delegation) ship as documented.
