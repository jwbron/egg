---
name: egg-sdlc
description: "Run the full egg SDLC stack natively in Claude Code (substrate-swap walking-skeleton for #2623). Boots the real `egg_orchestrator` in-process, dispatches the refiner role via Claude Code's Agent tool, enforces role file-write restrictions via a PreToolUse hook, and renders HITL decisions through `AskUserQuestion`. Walking-skeleton scope: refiner role only — plan / implement / pr roles are deferred to the follow-up issue."
disable-model-invocation: true
argument-hint: "[issue# | issue-url] [--repo owner/name]"
allowed-tools: Agent Read Write Edit AskUserQuestion Bash(gh issue view:*) Bash(gh issue list:*) Bash(git -C * remote:*) Bash(git remote:*) Bash(mkdir:*) Bash(ls:*) Bash(test:*) Bash(find:*) Bash(python3 *:*) Bash(cat:*) Bash(cp:*)
---

# egg-sdlc — full egg SDLC stack inside Claude Code (walking-skeleton)

This skill is the **claude-code-substrate** entry point for the real `egg_orchestrator` stack — the destination of the [substrate-swap ADR](../../../../docs/architecture/claude-code-substrate.md) shipped for [#2623](https://github.com/jwbron/egg/issues/2623). It is **not** a parallel Markdown approximation of egg's BRC like `plugins/refine-plan/`; it is the real orchestrator running in-process to the parent Claude Code session.

> **Walking-skeleton scope.** Per **cq-11 = "Spike then plan"**, this skill exercises **the refiner role only**. The plan, implement, and pr phases — and the rest of the role roster (`reviewer_refine`, `reviewer_agent_design`, `architect`, `task_planner`, `risk_analyst`, `coder`, `tester`, `documenter`, `reviewer_code`, `reviewer_contract`, …) — are explicitly out of scope for this spike. The follow-up issue extends the substrate to plan / implement / pr (see [the ADR](../../../../docs/architecture/claude-code-substrate.md#follow-up-issue-draft-reviewer-pasted-not-auto-filed)). If you call this skill with anything beyond a single refine phase, expect `NotImplementedError` and a pointer to the follow-up.

## What this gets you

- Real `egg_orchestrator` running in-process to your Claude Code session — no k3s, no Redis, no Docker, no gateway sidecar.
- The refiner subagent runs via Claude Code's `Agent` tool with `subagent_type: "general-purpose"` and a system prompt assembled by the real `build_system_prompt(sources)` (`shared/egg_harness/prompt.py:24`) — the structural depth fix from #2622.
- Role file-write restrictions are enforced at write time by a PreToolUse hook that imports `build_agent_patterns` from `shared/egg_restrictions/patterns.py:768` — the same source of truth the gateway uses for `403 restricted_path_modified`.
- HITL decisions surface through the parent session via `AskUserQuestion` and resume the orchestrator via `generator.send(...)` — cq-7 heredoc-style synchronous HITL.
- The refine artifact lands at the canonical egg path: `.egg-state/drafts/<issue>-analysis.md` (same path the k3s substrate writes).

## Install

The skill depends on the egg Python packages. **You install those once via pip**, then this skill imports them. Per cq-8 the plugin metadata declares the pip dependency name — the canonical string is in `plugins/egg-sdlc/.claude-plugin/plugin.json`.

```bash
# Whatever pip name the plugin metadata declares — the canonical string lives
# in `plugins/egg-sdlc/.claude-plugin/plugin.json`. (cq-12 settles the exact
# name; until then the plugin metadata may carry a TODO placeholder pointing
# back to these docs.)
pip install <pip-name-from-plugin.json>
```

The skill's pre-flight check imports `egg_orchestrator`; if that import fails, the skill emits a clear install instruction with the exact pip command and exits — it does NOT try to recover silently. **The install-error message in the pre-flight helper must match the install instruction in this SKILL.md** (TASK-1-7 acceptance).

**Python version.** Egg targets Python 3.11+. If your Claude Code session resolves to an older Python, the import will fail with a version error — re-run the install command in a 3.11+ venv.

**Marketplace footprint** stays well under the soft ~100 MB cap (feedback Q3). No new third-party dependencies were introduced for this substrate beyond what egg already declares.

## Trust-context note (read this before running)

This skill **runs egg in-process to your Claude Code session**. The session holds the real Anthropic API key, and every subagent spawned by the orchestrator inherits the session's credential context. In the k3s substrate the sandbox NEVER sees the real key — the gateway injects credentials server-side. In this substrate that boundary moves.

**Threat model**. The substrate is intended for **repos you already trust to commit on**. The threat is not "agent from a randomly-encountered issue"; it is your own SDLC pipeline. A subagent compromised via prompt injection (untrusted issue body, malicious PR content) can in principle read or exfiltrate the API key from environment / disk / network — the PreToolUse hook gates *the agent's own tool calls* but cannot prevent the agent from reading env vars.

If you need the gateway-isolated credential boundary, **keep using the k3s substrate** — it remains supported indefinitely (cq-9). Set `EGG_SUBSTRATE=k3s` (the default) and run the orchestrator HTTP daemon as before.

See the ADR's [Trust-context shift (R1)](../../../../docs/architecture/claude-code-substrate.md#trust-context-shift-r1) section for the full discussion of what changed, why it's accepted, and what's mitigated.

## Usage

```bash
/egg-sdlc 1234              # GitHub issue number (curated spike target)
/egg-sdlc #1234             # same
/egg-sdlc 1234 --repo jwbron/egg
```

### What the skill does

1. **Pre-flight check**. Imports `egg_orchestrator`. If the import fails, prints the install instruction (verbatim from the section above) and exits.
2. **Resolve repo + issue**. Picks up the repo from `--repo`, falls back to `git -C "$EGG_REPO_PATH" remote get-url origin`, falls back to cwd. Fetches the issue body once with `gh issue view <N>`.
3. **Boot the in-process orchestrator** by calling `run_pipeline_in_process(...)` (from `orchestrator/substrate/in_process.py`) with `EGG_SUBSTRATE=claude-code`. The function is a Python generator.
4. **Drive the generator**. Each value yielded is an `HITLDecision` object (`orchestrator/models.py:300`). The skill renders each via `AskUserQuestion`, sends the user's answer back via `generator.send(...)`, and the orchestrator resumes.
5. **Refiner runs**. The `ClaudeCodeSpawner` dispatches the refiner role via the `Agent` tool with `subagent_type: "general-purpose"`. The refiner runs inside a worktree under `.egg-state/<pipeline_id>/<repo>/`, writes its analysis to `.egg-state/drafts/<issue>-analysis.md`, and returns.
6. **Refine artifact lands**. The generator returns the analysis path; the skill prints a summary (recommended option, top open questions) and asks the refine HITL gate (approve / request changes / change approach / stop).
7. **Walking-skeleton fence**. If the operator chooses "approve and continue to plan", the skill currently raises `NotImplementedError` with a pointer to the follow-up issue — plan / implement / pr phases are out of scope for this spike.

### The heredoc-HITL loop (user-facing contract)

This is the load-bearing piece of cq-7. The orchestrator pauses at each HITL boundary by **yielding** an `HITLDecision`; the parent session decides how to surface the decision and feeds the answer back. The loop shape is:

```python
# Pseudocode of what the skill does internally. The actual call lives behind
# this skill's outer-loop logic — you do not write this yourself.
from orchestrator.substrate.in_process import run_pipeline_in_process

generator = run_pipeline_in_process(
    pipeline_id="issue-1234",
    repo="jwbron/egg",
    issue_number=1234,
    env={"EGG_SUBSTRATE": "claude-code"},
)

answer = None
while True:
    try:
        decision = generator.send(answer)
    except StopIteration as stop:
        analysis_path = stop.value
        break
    # decision is an HITLDecision; the skill renders it via AskUserQuestion.
    answer = ask_user_question_with(decision)
```

While the generator is paused at a yield boundary, the orchestrator's background threads (heartbeat poll, BRC re-review, message-bus tick) keep running so a long-paused HITL does NOT cause stuck-phase-transition alerts. Dropping the generator mid-cycle (`del generator` or session exit) joins the background threads cleanly via `GeneratorExit` — no leaked threads.

### Worktree layout

Per cq-5 the substrate ports egg's `WORKTREE_BASE_DIR` model:

```
.egg-state/
  <pipeline_id>/
    <repo>/          # per-pipeline shared checkout
      ...            # per-agent worktrees, branches, refs
  drafts/
    <issue>-analysis.md
  contracts/
    <pipeline_id>.json
  checkpoints/
    ...
```

The base path defaults to `~/.egg-worktrees/` (matching `gateway/worktree_manager.py:49`) but `EGG_WORKTREE_BASE` overrides. Path-escape safety mirrors the existing `is_relative_to` defense in the gateway (`gateway/worktree_manager.py:1711`) so a malicious pipeline ID can't escape the base.

### PreToolUse hook (file-write restrictions)

The substrate ships a Python hook entry script at `orchestrator/substrate/claude_code/hook_entry.py` and a `.claude/settings.json` template at `orchestrator/substrate/claude_code/settings.template.json`. You activate the hook by **copying** the template into your own `.claude/settings.json` — it is **not** silently activated by the plugin install.

What the hook does:

1. Reads tool name + tool input from stdin (per the Claude Code PreToolUse contract).
2. Imports `build_agent_patterns` from `shared/egg_restrictions/patterns.py:768`.
3. Emits `deny` + `message` JSON to stdout when the write target is outside the caller's role's allow-list. The `message` mirrors the gateway's `check_agent_restrictions` denial format (`gateway/phase_filter.py:1061`) so the error you see in the Claude Code UI matches what k3s users see in their gateway logs.

The hook reads the calling role from `EGG_AGENT_ROLE` in the env. **Open empirical question (R2)**: whether Claude Code reliably resolves which subagent is invoking a tool from the hook's process context for *nested* subagent dispatch. If the spike evidence shows the hook cannot reliably resolve the role for a nested subagent, the documented fallback is **MCP-validator-side enforcement** (cq-6 option 2) — the substrate keeps `patterns.py` as the source of truth and adds MCP-tool-side validators. The follow-up issue inherits the empirical question.

### What's NOT in this skill

A non-exhaustive list of capabilities you would expect from the full SDLC and that this walking-skeleton intentionally does not ship:

- Plan / implement / pr phases. The skill is refine-only.
- Other refine-team roles (`reviewer_refine`, `reviewer_agent_design`). The refiner runs solo here; reviewer feedback is not collected on this substrate yet.
- Real BRC concurrency over multi-producer / multi-reviewer cycles. The `InProcessMessageBus` preserves the invariants needed to run BRC, but a single-role spike does not exercise them end-to-end.
- Cost cap (`EGG_PIPELINE_MAX_AGENT_INVOCATIONS`). Recommended in the ADR (REC5); not implemented in the spike.
- Custom `subagent_type` per-role agent definitions in `.claude/agents/<role>.md` (R15 model (b)). The skill uses `subagent_type: "general-purpose"` for now; per-role tool restrictions rely on the PreToolUse hook + prompt discipline.
- `EggHarnessSpawner` for headless / CLI mode (feedback Q4). Reserved for the follow-up.
- `egg-state prune` verb for local checkpoint cleanup (feedback Q6). Reserved for the follow-up.
- Fork-based sub-task delegation (cq-10's deferred half). The substrate ships only the checkpoint half.

For each of these, see the [Follow-up issue draft](../../../../docs/architecture/claude-code-substrate.md#follow-up-issue-draft-reviewer-pasted-not-auto-filed) in the ADR.

## Compatibility with the k3s substrate

`EGG_SUBSTRATE` selects the substrate at orchestrator boot:

| `EGG_SUBSTRATE` | What you get |
|---|---|
| unset, `""`, or `"k3s"` | The k3s pipeline as before (`KubernetesSpawner` + `RedisMessageStore` + gateway sidecar). The HTTP daemon entry (`orchestrator/cli.py:83 cmd_serve`) remains the boot path |
| `"claude-code"` | This skill's in-process model (`ClaudeCodeSpawner` + `InProcessMessageBus` + `PreToolUseHookPolicy` + `LocalWorktreeManager`). `run_pipeline_in_process(...)` is the boot path |

The contract schema (`shared/egg_contracts/models.py::Contract` v1.1), BRC history (`.egg-state/brc-history/<id>-<phase>.json`), agent outputs (`.egg-state/agent-outputs/`), and drafts (`.egg-state/drafts/<id>-analysis.md`) are all filesystem-native and substrate-portable. A pipeline started on one substrate can have its contract read by the other.

## Failure modes and diagnostics

- **`ImportError: No module named 'egg_orchestrator'`**: the pre-flight check failed. Re-run the pip install command above.
- **`NotImplementedError: claude-code substrate runs refine only`**: you tried to advance past the refine HITL gate. The spike is refine-only; plan / implement / pr live in the follow-up.
- **`NotImplementedError: EGG_SUBSTRATE=k3s requires the HTTP daemon`** (raised from `run_pipeline_in_process`): you set `EGG_SUBSTRATE=k3s` while running this in-process skill. k3s users use `orchestrator/cli.py:83 cmd_serve`, not the skill.
- **PreToolUse hook denies a write the role *should* be allowed**: the `settings.template.json` is wired against a stale or wrong `EGG_AGENT_ROLE`. The hook prints which role it saw — re-check the spawn env.
- **HITL takes a long time and you see no progress**: the orchestrator's background threads continue while the generator is paused; the heartbeat-during-HITL acceptance criterion guarantees this. If you genuinely want to abandon the run, drop the generator (or close the session); `GeneratorExit` joins the background threads cleanly.

## Where this fits

- [Substrate-swap ADR](../../../../docs/architecture/claude-code-substrate.md) — the canonical reference for the four interfaces, the implementations, the eleven cq decisions, and the deferred work.
- [`plugins/refine-plan/`](../../../refine-plan/) — the earlier Markdown-only approximation of egg's refine + plan phases. This skill **supersedes** that for solo-developer use of the real orchestrator; `refine-plan` remains as a portable Python-deps-free alternative.
- [Concurrent execution guide](../../../../docs/guides/concurrent-execution.md) — the BRC protocol the substrate preserves.
- [Integration-test trust boundary](../../../../docs/architecture/integration-test-trust-boundary.md) — names "in-parent-Claude-Code-session" as a new trust context (R1).
