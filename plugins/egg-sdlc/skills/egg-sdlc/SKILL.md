---
name: egg-sdlc
description: "Run the full egg SDLC stack natively in Claude Code (substrate-swap rollout from #2623 → #2717). Target shape: boot the real `egg_orchestrator` in-process, dispatch role subagents via Claude Code's Agent tool, enforce role file-write restrictions via a PreToolUse hook, and render HITL decisions through `AskUserQuestion`. Refine-phase scope landed in slice 1 of the #2717 rollout: refiner + reviewer_refine + reviewer_agent_design, driven by a flattened `bin/run_pipeline.py` stage driver that ferries a single `pending_hitl` envelope through `.egg-state/contracts/<id>.json` per skill→Python round-trip. Plan / implement / pr phases land in later slices of the rollout."
disable-model-invocation: true
argument-hint: "[issue# | issue-url] [--repo owner/name]"
allowed-tools: Agent Read AskUserQuestion Bash(gh issue view:*) Bash(gh issue list:*) Bash(git -C * remote:*) Bash(git remote:*) Bash(mkdir:*) Bash(ls:*) Bash(test:*) Bash(find:*) Bash(python3 *:*) Bash(cat:*) Bash(cp:*)
---

# egg-sdlc — full egg SDLC stack inside Claude Code

This skill is the **claude-code-substrate** entry point for the real `egg_orchestrator` stack — the user-facing entry point for the [substrate-swap ADR](../../../../docs/architecture/claude-code-substrate.md) seeded by the walking-skeleton spike [#2623](https://github.com/jwbron/egg/issues/2623) and being rolled out under [#2717](https://github.com/jwbron/egg/issues/2717). It is **not** a parallel Markdown approximation of egg's BRC like `plugins/refine-plan/`; it is the real orchestrator running in-process to the parent Claude Code session.

> **Rollout status (slice 1 of #2717 landed).** The refine phase now exercises the full refine-team roster on this substrate: `refiner` + `reviewer_refine` + `reviewer_agent_design` (the third is spawned only when the target repo is `jwbron/egg`). The heredoc-HITL bridge gap that the original spike deferred is **closed for refine-phase** via the flattened `bin/run_pipeline.py` stage driver (see "How the flattened bridge works" below). The plan / implement / pr phases — and their role rosters (`architect`, `task_planner`, `risk_analyst`, `reviewer_plan`, `coder`, `tester`, `documenter`, `reviewer_code`, `reviewer_contract`, …) — land in later slices of the #2717 rollout (slice 2 = plan, slice 3 = implement, slice 4 = pr, slice 5 = hardening). If you call this skill with anything beyond refine today, expect `NotImplementedError` and a pointer to the next slice.

## What this gets you

- Real `egg_orchestrator` running in-process to your Claude Code session — no k3s, no Redis, no Docker, no gateway sidecar.
- Refine-team subagents run via Claude Code's `Agent` tool with `subagent_type: "general-purpose"` and a system prompt assembled by the real `build_system_prompt(sources)` (`shared/egg_harness/prompt.py:24`) — the structural depth fix from #2622. The refiner + the two refine reviewers each pick up their role rubric from `agents/<role>.md` automatically.
- Role file-write restrictions are enforced at write time by a PreToolUse hook that imports `build_agent_patterns` from `shared/egg_restrictions/patterns.py:768` — the same source of truth the gateway uses for `403 restricted_path_modified`.
- HITL decisions surface through the parent session via `AskUserQuestion` and resume the orchestrator from where it paused — the flattened `bin/run_pipeline.py` stage driver round-trips each `HITLDecision` through `.egg-state/contracts/<id>.json#pending_hitl` so the skill can drive a generator-yielding orchestrator from Bash steps without keeping a Python process alive across yields.
- The refine artifact lands at the canonical egg path: `.egg-state/drafts/<issue>-analysis.md` (same path the k3s substrate writes); reviewer verdicts land at `.egg-state/agent-outputs/<issue>-<reviewer>-output.json`.

## Install

The skill depends on the egg Python packages. **Until cq-12 resolves and publishes a pip-installable package, the install is from source.** The plugin metadata's `egg.install_instructions` field carries the same from-source command the preflight prints on import failure — both surfaces stay in sync via the same source of truth.

```bash
git clone https://github.com/jwbron/egg.git
cd egg
pip install -r requirements.txt
export PYTHONPATH="$PWD:$PWD/shared:$PYTHONPATH"
```

The skill's pre-flight check imports `orchestrator.substrate.in_process.run_pipeline_in_process`; if that import fails, the skill emits the same from-source instructions and exits — it does NOT try to recover silently. **The install-error message in the pre-flight helper reads from the same `plugin.json` field this section documents** so the two surfaces remain consistent (TASK-1-7 acceptance). The follow-up issue (see the substrate ADR) tracks publishing a `pip install`-able package; until then, the from-source path is the only supported install.

**Python version.** Egg targets Python 3.11+. If your Claude Code session resolves to an older Python, the import will fail with a version error — re-run the install command in a 3.11+ venv.

**Marketplace footprint** stays well under the soft ~100 MB cap (feedback Q3). No new third-party dependencies were introduced for this substrate beyond what egg already declares.

## Trust-context note (read this before running)

This skill **runs egg in-process to your Claude Code session**. The session holds the real Anthropic API key, and every subagent spawned by the orchestrator inherits the session's credential context. In the k3s substrate the sandbox NEVER sees the real key — the gateway injects credentials server-side. In this substrate that boundary moves.

**Threat model**. The substrate is intended for **repos you already trust to commit on**. The threat is not "agent from a randomly-encountered issue"; it is your own SDLC pipeline. A subagent compromised via prompt injection (untrusted issue body, malicious PR content) can in principle read or exfiltrate the API key from environment / disk / network — the PreToolUse hook gates *the agent's own tool calls* but cannot prevent the agent from reading env vars.

If you need the gateway-isolated credential boundary, **keep using the k3s substrate** — it remains supported indefinitely (cq-9). Set `EGG_SUBSTRATE=k3s` (the default) and run the orchestrator HTTP daemon as before.

See the ADR's [Trust-context shift (R1)](../../../../docs/architecture/claude-code-substrate.md#trust-context-shift-r1) section for the full discussion of what changed, why it's accepted, and what's mitigated.

## Usage

```bash
/egg-sdlc 1234              # GitHub issue number (curated rollout target)
/egg-sdlc #1234             # same
/egg-sdlc 1234 --repo jwbron/egg
```

### What the skill does

1. **Pre-flight check**. Imports `egg_orchestrator`. If the import fails, prints the install instruction (verbatim from the section above) and exits. _(`bin/preflight.py`.)_
2. **Resolve repo + issue**. Picks up the repo from `--repo`, falls back to `git -C "$EGG_REPO_PATH" remote get-url origin`, falls back to cwd. Fetches the issue body once with `gh issue view <N>`.
3. **Boot the in-process orchestrator** by invoking the flattened stage driver `bin/run_pipeline.py` for the first time, passing the repo / issue / pipeline id. The driver imports `run_pipeline_in_process(...)` from `orchestrator/substrate/in_process.py`, advances the generator to its next `HITLDecision` yield, serialises the decision into `.egg-state/contracts/<id>.json#pending_hitl.decision`, and exits.
4. **Render the decision**. The skill reads `pending_hitl.decision` and surfaces it via `AskUserQuestion`. The operator's selected option is written back to `pending_hitl.answer`.
5. **Resume the orchestrator**. The skill re-invokes `bin/run_pipeline.py`. The driver loads pipeline state from the contract file, calls `generator.send(answer)`, advances to the next yield (or to `StopIteration`), serialises the next decision, and exits. The skill loops back to step 4 until the driver signals "no more decisions" (the generator completed) or the operator chooses a terminating answer.
6. **Refine subagents run inside step 3 / 5.** The `ClaudeCodeSpawner` dispatches the three refine-team roles via the `Agent` tool with `subagent_type: "general-purpose"`. Each subagent runs inside a worktree under `<EGG_WORKTREE_BASE>/<pipeline_id>/<role>/` (default base `~/.egg-worktrees/`), the refiner writes its analysis to `.egg-state/drafts/<issue>-analysis.md`, each reviewer writes its verdict to `.egg-state/agent-outputs/<issue>-<reviewer>-output.json`. The orchestrator coordinates ACK / NACK / re-propose cycles via the in-process message bus before pausing at the refine HITL gate.
7. **Refine HITL gate**. The skill surfaces a refine-gate `HITLDecision` (approve / request changes / change approach / stop) alongside the refiner's recommended option, the top open questions, and each reviewer's ACK or NACK summary.
8. **Phase fence**. If the operator chooses "approve and continue to plan", the skill currently raises `NotImplementedError` with a pointer to slice 2 of the #2717 rollout — plan / implement / pr phases are out of scope until later slices land.

### How the flattened bridge works

The orchestrator's `run_pipeline_in_process(...)` is a Python generator that pauses at each HITL boundary by **yielding** an `HITLDecision`. A Claude Code skill cannot keep a single long-lived Python process alive across multiple `AskUserQuestion` round-trips — every `python3` invocation from a Bash skill step is a fresh process whose generator state dies at exit. Per cq-1 = hybrid (Option C), this skill picks the **flattened** option for refine and plan phases (the daemon variant lives in slice 3 for implement-phase concurrency): a hand-shaped sequence of single-yield `python3 bin/run_pipeline.py` invocations that thread decisions and answers through `.egg-state/contracts/<id>.json#pending_hitl`.

The single-yield carrier is the **`pending_hitl` envelope**. Its shape is the load-bearing state-serialization contract between this driver and the future daemon variant — the daemon-mode driver in slice 3 consumes the same envelope shape, so reviewers can compare contract files across the two bridges 1:1.

```json
{
  "pending_hitl": {
    "version": 1,
    "pipeline_id": "issue-1234",
    "timestamp": "<ISO-8601 UTC>",
    "decision": {
      "question": "...",
      "options": [{"label": "...", "description": "..."}, ...],
      "phase": "refine",
      ...
    },
    "answer": null
  }
}
```

The skill loop:

```bash
# Iteration N: ask the orchestrator for the next decision.
python3 plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py \
    --pipeline-id "issue-${ISSUE}" \
    --repo "${REPO}" \
    --issue "${ISSUE}"

# Read pending_hitl.decision out of the contract; render via AskUserQuestion;
# write the operator's selected option back to pending_hitl.answer.

# Iteration N+1: feed the answer back. Same invocation; the driver picks up
# pending_hitl.answer, calls generator.send(answer), serialises the next yield.
python3 plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py \
    --pipeline-id "issue-${ISSUE}" \
    --repo "${REPO}" \
    --issue "${ISSUE}"

# Repeat until the driver reports `pending_hitl.decision == null` (generator
# completed) or the operator's answer was a terminating one.
```

Each `bin/run_pipeline.py` invocation:

1. Loads `.egg-state/contracts/<id>.json` (the orchestrator-managed pipeline state).
2. If `pending_hitl.answer` is set, advances the generator via `generator.send(answer)` and clears the answer. Otherwise this is the first invocation; it starts the generator.
3. On the next yield, writes the new `HITLDecision` into `pending_hitl.decision` (with `answer = null`, bumped `version`, and a fresh `timestamp`) and exits 0.
4. On `StopIteration`, clears `pending_hitl.decision` (signals "no more decisions") and writes the analysis path / completion summary into the contract, then exits 0.
5. On internal error, writes a diagnostic into the contract's failure log and exits 1.

**The `pending_hitl` envelope is a stable contract**: the `version`, `pipeline_id`, `timestamp`, `decision`, and `answer` fields must remain shape-compatible with the daemon-variant driver (slice 3 = TASK-3-2) so a pipeline started on one bridge can be resumed on the other. The driver's top-of-file comment names this contract explicitly.

While the generator is paused at a yield boundary, the orchestrator's background threads (heartbeat poll, BRC re-review, message-bus tick) inside the current `bin/run_pipeline.py` invocation join cleanly via `GeneratorExit` when the process exits — no leaked threads across the skill→Python boundary.

### Worktree layout

Per cq-5 the substrate ports egg's `WORKTREE_BASE_DIR` model. There are two filesystem trees: **worktrees** (per-agent git checkouts) and **state** (drafts, contracts, agent-outputs, checkpoints). They live under separate roots by default.

**Worktrees** default to `~/.egg-worktrees/<pipeline_id>/<role>/` (matching the shape at `gateway/worktree_manager.py:49`, which hardcodes `/home/egg/.egg-worktrees` for the gateway container — the substrate's `LocalWorktreeManager` expands `~` against the calling user's `$HOME`). Each per-role worktree gets its own `git worktree` checked out on `egg/<pipeline_id>/<role>`. `EGG_WORKTREE_BASE` overrides the root; the typical override is to point it at `./.egg-state/` so worktrees and state live in one tree.

```
# Default layout (no EGG_WORKTREE_BASE override)
~/.egg-worktrees/
  <pipeline_id>/
    <role>/          # per-role worktree on branch egg/<pipeline_id>/<role>

.egg-state/           # state files (relative to the in-process orchestrator's CWD)
  drafts/
    <issue>-analysis.md
  contracts/
    <pipeline_id>.json
  agent-outputs/
    <issue>-<role>-output.json
  brc-history/
    <id>-<phase>.{md,json}
  checkpoints/
    <pipeline_id>/   # per-pipeline checkpoint shard
      ...
```

```
# Typical override: EGG_WORKTREE_BASE=./.egg-state/
.egg-state/
  <pipeline_id>/
    <role>/          # per-role worktrees moved alongside state
  drafts/
  contracts/
  agent-outputs/
  brc-history/
  checkpoints/
    <pipeline_id>/
```

Path-escape safety mirrors the existing `is_relative_to` + `resolve()` defense in the gateway (`gateway/worktree_manager.py:1700-1711`, within `list_orphan_worktree_dirs`) so a malicious pipeline ID can't escape the base.

### PreToolUse hook (file-write restrictions)

The substrate ships a Python hook entry script at `orchestrator/substrate/claude_code/hook_entry.py` and a `.claude/settings.json` template at `orchestrator/substrate/claude_code/settings.template.json`. You activate the hook by **copying** the template into your own `.claude/settings.json` — it is **not** silently activated by the plugin install.

What the hook does:

1. Reads tool name + tool input from stdin (per the Claude Code PreToolUse contract).
2. Imports `build_agent_patterns` from `shared/egg_restrictions/patterns.py:768`.
3. Emits `deny` + `message` JSON to stdout when the write target is outside the caller's role's allow-list. The `message` mirrors the gateway's `check_agent_restrictions` denial format (`gateway/phase_filter.py:1061`) so the error you see in the Claude Code UI matches what k3s users see in their gateway logs.

The hook reads the calling role from `EGG_AGENT_ROLE` in the env. **R2 — nested-dispatch role-routing**: slice 1 of #2717 lands a 2-subagent worked example at `integration_tests/regression/test_pretooluse_hook_nested.py` (TASK-1-5) that drives the hook through a parent → child Agent-tool dispatch via the test-only fake at `integration_tests/regression/_agent_tool_fake.py` (TASK-1-9). The verdict is recorded to `.egg-state/<pipeline_id>/r2-verdict.json`. Note that the production substrate runs subagents through the harness re-host (`ClaudeCodeSpawner` per cq-3) rather than Agent-tool dispatch, so R2 today validates hook *logic* (given accurate `EGG_AGENT_ROLE` propagation) and becomes load-bearing only if cq-3 flips to Agent-tool dispatch in a future issue. If the verdict is `fail`, slice 5 wires the documented fallback — **MCP-validator-side enforcement** (cq-6 option 2) — the substrate keeps `patterns.py` as the source of truth and adds agent-side policy enforcement at `sandbox/egg_agent_tools/handlers/restrictions.py`.

### What's NOT in this skill (yet)

A non-exhaustive list of capabilities that the substrate-swap rollout targets but slice 1 of #2717 has not landed:

- **Plan / implement / pr phases.** Slice 1 lands the bridge + refine reviewers; slice 2 lands the plan-phase substrate (3 producers + 1 reviewer); slice 3 lands the implement-phase substrate + daemon HITL bridge; slice 4 lands the pr-phase substrate + the rest of the conformance matrix. If you advance past the refine HITL gate today, the skill raises `NotImplementedError` with a pointer to the active slice.
- **Cost cap (`EGG_PIPELINE_MAX_AGENT_INVOCATIONS`).** Recommended in the ADR (REC5); lands in slice 5 of the #2717 rollout.
- **Custom `subagent_type` per-role agent definitions in `.claude/agents/<role>.md` (R15 model (b)).** The skill uses `subagent_type: "general-purpose"` for now; per-role tool restrictions rely on the PreToolUse hook + prompt discipline. Migration is **contingent on the R2 verdict** (TASK-1-5): if the hook reliably resolves role under nested dispatch, slice 5 stays on model (a); if not, slice 5 migrates every role rubric to a real `.claude/agents/<role>.md` definition and adds agent-side policy enforcement (cq-6 option 2). The R2 verdict file at `.egg-state/<pipeline_id>/r2-verdict.json` records the empirical result.
- **`EggHarnessSpawner` for headless / CLI mode (feedback Q4).** Lands in slice 5.
- **`egg-state prune` verb for local checkpoint cleanup (feedback Q6).** Reserved for the follow-up issue beyond #2717.
- **Fork-based sub-task delegation (cq-10's deferred half).** Lands in slice 5.

For each of these, see the [Substrate-swap ADR](../../../../docs/architecture/claude-code-substrate.md) and the [`#2717` plan](https://github.com/jwbron/egg/issues/2717) for the slice-DAG breakdown.

## Compatibility with the k3s substrate

`EGG_SUBSTRATE` selects the substrate at orchestrator boot:

| `EGG_SUBSTRATE` | What you get |
|---|---|
| unset, `""`, or `"k3s"` | The k3s pipeline as before (`KubernetesSpawner` + `RedisMessageStore` + gateway sidecar). The HTTP daemon entry (`orchestrator/cli.py:83 cmd_serve`) remains the boot path |
| `"claude-code"` | This skill's in-process model (`ClaudeCodeSpawner` + `InProcessMessageBus` + `PreToolUseHookPolicy` + `LocalWorktreeManager`). `run_pipeline_in_process(...)` is the boot path |

The contract schema (`shared/egg_contracts/models.py::Contract` v1.1), BRC history (`.egg-state/brc-history/<id>-<phase>.json`), agent outputs (`.egg-state/agent-outputs/`), and drafts (`.egg-state/drafts/<id>-analysis.md`) are all filesystem-native and substrate-portable. A pipeline started on one substrate can have its contract read by the other.

## Failure modes and diagnostics

- **`ImportError: No module named 'egg_orchestrator'`**: the pre-flight check failed. Re-run the pip install command above.
- **`NotImplementedError: claude-code substrate runs refine only`**: you tried to advance past the refine HITL gate. Slice 1 of the #2717 rollout is refine-only; plan / implement / pr land in slices 2 / 3 / 4 of the same rollout.
- **`NotImplementedError: EGG_SUBSTRATE=k3s requires the HTTP daemon`** (raised from `run_pipeline_in_process`): you set `EGG_SUBSTRATE=k3s` while running this in-process skill. k3s users use `orchestrator/cli.py:83 cmd_serve`, not the skill.
- **PreToolUse hook denies a write the role *should* be allowed**: the `settings.template.json` is wired against a stale or wrong `EGG_AGENT_ROLE`. The hook prints which role it saw — re-check the spawn env.
- **HITL takes a long time and you see no progress**: the orchestrator's background threads keep running inside each `bin/run_pipeline.py` invocation while the generator is paused on a yield; the heartbeat-during-HITL acceptance criterion guarantees this within an invocation. Between invocations (i.e. while the skill is rendering `AskUserQuestion` and waiting on the operator), the Python process has exited and the orchestrator state lives only in `.egg-state/contracts/<id>.json#pending_hitl`. If you genuinely want to abandon the run, close the session; the next invocation of `bin/run_pipeline.py` will resume from the contract file, or you can delete the contract file to discard the run entirely.

- **`pending_hitl.decision` is `null` after a `bin/run_pipeline.py` invocation**: the generator returned (`StopIteration`); the phase is complete and the analysis path is recorded on the contract. The skill loop should exit, not call `bin/run_pipeline.py` again.

## Where this fits

- [Substrate-swap ADR](../../../../docs/architecture/claude-code-substrate.md) — the canonical reference for the four interfaces, the implementations, the eleven cq decisions, and the deferred-vs-landed status of each rollout item.
- [`plugins/refine-plan/`](../../../refine-plan/) — the earlier Markdown-only approximation of egg's refine + plan phases. This skill **supersedes** that for solo-developer use of the real orchestrator; `refine-plan` remains as a portable Python-deps-free alternative.
- [Concurrent execution guide](../../../../docs/guides/concurrent-execution.md) — the BRC protocol the substrate preserves.
- [Integration-test trust boundary](../../../../docs/architecture/integration-test-trust-boundary.md) — names "in-parent-Claude-Code-session" as a new trust context (R1).
