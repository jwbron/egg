---
name: egg-sdlc
description: "Run the full egg SDLC stack natively in Claude Code (substrate-swap rollout from #2623 → #2717). Target shape: boot the real `egg_orchestrator` in-process, dispatch role subagents via Claude Code's Agent tool, enforce role file-write restrictions via a PreToolUse hook, and render HITL decisions through `AskUserQuestion`. Refine-phase scope landed in slice 1 of the #2717 rollout (refiner + reviewer_refine + reviewer_agent_design); plan-phase scope landed in slice 2 (architect + task_planner + risk_analyst + reviewer_plan). Both phases are driven by the flattened `bin/run_pipeline.py` stage driver that ferries a single `pending_hitl` envelope through `.egg-state/contracts/<id>.json` per skill→Python round-trip. Implement / pr phases land in later slices of the rollout."
disable-model-invocation: true
argument-hint: "[issue# | issue-url] [--repo owner/name]"
allowed-tools: Agent Read AskUserQuestion Bash(gh issue view:*) Bash(gh issue list:*) Bash(git -C * remote:*) Bash(git remote:*) Bash(mkdir:*) Bash(ls:*) Bash(test:*) Bash(find:*) Bash(python3 plugins/egg-sdlc/skills/egg-sdlc/bin/*:*) Bash(cat:*) Bash(cp:*)
---

# egg-sdlc — full egg SDLC stack inside Claude Code

This skill is the **claude-code-substrate** entry point for the real `egg_orchestrator` stack — the user-facing entry point for the [substrate-swap ADR](../../../../docs/architecture/claude-code-substrate.md) seeded by the walking-skeleton spike [#2623](https://github.com/jwbron/egg/issues/2623) and being rolled out under [#2717](https://github.com/jwbron/egg/issues/2717). It is **not** a parallel Markdown approximation of egg's BRC like `plugins/refine-plan/`; it is the real orchestrator running in-process to the parent Claude Code session.

> **Rollout status (slices 1 + 2 of #2717 landed).** The refine and plan phases now exercise their full role rosters on this substrate:
>
> - **Refine** — `refiner` + `reviewer_refine` + `reviewer_agent_design` (the third is spawned only when the target repo is `jwbron/egg`). _(Slice 1.)_
> - **Plan** — `architect` (runs solo first) + `task_planner` and `risk_analyst` (run concurrently downstream of the architect) + `reviewer_plan` (ACKs / NACKs each of the three producer edges). The stage yields a plan-HITL decision after `CONSENSUS_CONFIRMED` lands on every producer edge — see "Plan phase" below. _(Slice 2.)_
>
> The heredoc-HITL bridge gap that the original spike deferred is **closed for refine + plan** via the flattened `bin/run_pipeline.py` stage driver (see "How the flattened bridge works" below). The implement / pr phases — and their role rosters (`coder`, `tester`, `documenter`, `reviewer_code`, `reviewer_code_holistic`, `reviewer_contract`, `reviewer_security`, `reviewer_concurrency`) — land in later slices of the #2717 rollout (slice 3 = implement + daemon HITL bridge, slice 4 = pr + the rest of the conformance matrix, slice 5 = hardening). If you call this skill with anything beyond refine or plan today, expect `NotImplementedError` and a pointer to the next slice.

## What this gets you

- Real `egg_orchestrator` running in-process to your Claude Code session — no k3s, no Redis, no Docker, no gateway sidecar.
- Refine- and plan-team subagents run via Claude Code's `Agent` tool with `subagent_type: "general-purpose"` and a system prompt assembled by the real `build_system_prompt(sources)` (`shared/egg_harness/prompt.py:24`) — the structural depth fix from #2622. Each role (`refiner`, `reviewer_refine`, `reviewer_agent_design`, `architect`, `task_planner`, `risk_analyst`, `reviewer_plan`) picks up its rubric from `agents/<role>.md` automatically.
- Role file-write restrictions are enforced at write time by a PreToolUse hook that imports `build_agent_patterns` from `shared/egg_restrictions/patterns.py:768` — the same source of truth the gateway uses for `403 restricted_path_modified`.
- HITL decisions surface through the parent session via `AskUserQuestion` and resume the orchestrator from where it paused — the flattened `bin/run_pipeline.py` stage driver round-trips each `HITLDecision` through `.egg-state/contracts/<id>.json#pending_hitl` so the skill can drive a generator-yielding orchestrator from Bash steps without keeping a Python process alive across yields.
- Refine + plan artifacts land at the canonical egg paths: `.egg-state/drafts/<issue>-analysis.md` (refine) and `.egg-state/drafts/<issue>-plan.md` (plan); each role's handoff JSON / verdict JSON lands at `.egg-state/agent-outputs/<issue>-<role>-output.json`. These paths match the k3s substrate's writes.

## Install

The skill depends on the egg Python packages. **Until cq-12 resolves and publishes a pip-installable package, the install is from source.** The plugin metadata's `egg.install_instructions` field carries the same from-source command the preflight prints on import failure — both surfaces stay in sync via the same source of truth.

```bash
git clone https://github.com/jwbron/egg.git
cd egg
pip install .
export PYTHONPATH="$PWD:$PWD/shared:$PYTHONPATH"
```

The skill's pre-flight check imports `orchestrator.substrate.in_process.run_pipeline_in_process`; if that import fails, the skill emits the same from-source instructions and exits — it does NOT try to recover silently. **The install-error message in the pre-flight helper reads from the same `plugin.json` field this section documents** so the two surfaces remain consistent (TASK-1-7 acceptance). The follow-up issue (see the substrate ADR) tracks publishing a `pip install`-able package; until then, the from-source path is the only supported install.

**Python version.** Egg requires Python **3.14+** (per `pyproject.toml`'s `requires-python = ">=3.14"`). `pip install .` will refuse to install on older interpreters. If your Claude Code session resolves to an older Python, re-run the install command in a 3.14+ venv (e.g. `python3.14 -m venv .venv && source .venv/bin/activate && pip install .`).

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
3. **Boot the in-process orchestrator** by invoking the flattened stage driver `bin/run_pipeline.py` for the first time with the pipeline id as a positional arg plus `--repo` / `--issue-number` flags. The driver imports `run_pipeline_in_process(...)` from `orchestrator/substrate/in_process.py`, advances a fresh generator to its first `HITLDecision` yield, serialises the decision into `.egg-state/contracts/<id>.json#pending_hitl`, and exits 0.
4. **Render the decision**. The skill reads `pending_hitl.decision` and `pending_hitl.status` from the contract; when `status == "pending"` it surfaces the decision via `AskUserQuestion`. The operator's selected option is written back to `pending_hitl.answer` (and `status` is set to `answered`) via an inline `python3 -c "..."` invocation — see "How the flattened bridge works" below.
5. **Resume the orchestrator**. The skill re-invokes `bin/run_pipeline.py` with the same args. The driver promotes `pending_hitl.answer` into `answer_log`, replays the full `answer_log` into a fresh generator (deterministic replay — see "Generator state across invocations" below), advances to the next yield (or to `StopIteration`), serialises the next decision, and exits. The skill loops back to step 4 until `pending_hitl.status ∈ {completed, aborted, error}`.
6. **Refine subagents run inside step 3 / 5.** The `ClaudeCodeSpawner` dispatches the three refine-team roles via the `Agent` tool with `subagent_type: "general-purpose"`. Each subagent runs inside a worktree under `<EGG_WORKTREE_BASE>/<pipeline_id>/<role>/` (default base `~/.egg-worktrees/`), the refiner writes its analysis to `.egg-state/drafts/<issue>-analysis.md`, each reviewer writes its verdict to `.egg-state/agent-outputs/<issue>-<reviewer>-output.json`. The orchestrator coordinates ACK / NACK / re-propose cycles via the in-process message bus before pausing at the refine HITL gate.
7. **Refine HITL gate**. The skill surfaces a refine-gate `HITLDecision` (approve / request changes / change approach / stop) alongside the refiner's recommended option, the top open questions, and each reviewer's ACK or NACK summary.
8. **Plan subagents run inside the next driver invocation.** When the operator chooses "approve and continue to plan" at the refine gate, the next `bin/run_pipeline.py` invocation enters the plan stage. The `ClaudeCodeSpawner` dispatches `architect` solo first; once its handoff lands, `task_planner` and `risk_analyst` are spawned concurrently. `reviewer_plan` is spawned for the ACK / NACK cycle after each `CONSENSUS_PROPOSE`; the in-process message bus runs the open-NACK barrier the same way the k3s substrate does. The plan document lands at `.egg-state/drafts/<issue>-plan.md`; each role's handoff or verdict lands at `.egg-state/agent-outputs/<issue>-<role>-output.json`.
9. **Plan HITL gate.** Once `CONSENSUS_CONFIRMED` fires on all three producer edges (`architect → reviewer_plan`, `task_planner → reviewer_plan`, `risk_analyst → reviewer_plan`), the stage yields a plan-gate `HITLDecision` (approve / request changes / change approach / stop) alongside the architect's approach summary, the task_planner's slice DAG, the risk_analyst's top-3 risks and blocking concerns, and each per-edge reviewer verdict.
10. **Phase fence.** If the operator chooses "approve and continue to implement", the skill currently raises `NotImplementedError` with a pointer to slice 3 of the #2717 rollout — implement / pr phases land in slices 3 / 4 of the rollout.

### How the flattened bridge works

The orchestrator's `run_pipeline_in_process(...)` is a Python generator that pauses at each HITL boundary by **yielding** an `HITLDecision`. A Claude Code skill cannot keep a single long-lived Python process alive across multiple `AskUserQuestion` round-trips — every `python3` invocation from a Bash skill step is a fresh process whose generator state dies at exit. Per cq-1 = hybrid (Option C), this skill picks the **flattened** option for refine and plan phases (the daemon variant lives in slice 3 for implement-phase concurrency): a hand-shaped sequence of `python3 bin/run_pipeline.py` invocations that thread decisions and answers through `.egg-state/contracts/<id>.json#pending_hitl`.

The single-yield carrier is the **`pending_hitl` envelope**. Its shape is the load-bearing state-serialization contract between this driver and the future daemon variant — the daemon-mode driver in slice 3 (TASK-3-2) consumes the same envelope shape, so reviewers can compare contract files across the two bridges 1:1. The driver's top-of-file comment at `plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py:20-46` is the source of truth; this section mirrors it.

```json
{
  "pending_hitl": {
    "version": 1,
    "pipeline_id": "issue-1234",
    "timestamp": "<ISO-8601 UTC of last driver write>",
    "decision": {
      "question": "...",
      "options": [{"label": "...", "description": "..."}, ...],
      "phase": "refine",
      "...": "..."
    },
    "answer": null,
    "status": "pending",
    "result": null,
    "error": null,
    "answer_log": []
  }
}
```

Field semantics:

- `version` — schema version (currently `1`). Do not bump without coordinating with the slice-3 daemon variant; the field exists so a future schema bump can be detected by both bridges.
- `pipeline_id` — echoes `contract.pipeline_id` for sanity-checking.
- `timestamp` — ISO-8601 UTC of the last driver write.
- `decision` — the most recently yielded `HITLDecision`, serialised via `.model_dump(mode="json")` (pydantic) or `dict()` (fallback). `null` before the generator yields, and `null` again on `StopIteration`.
- `answer` — the operator's response to the current `decision`. The skill body writes this after rendering `AskUserQuestion`; the driver consumes it on its next invocation (promotes it into `answer_log` and clears `answer` back to `null`).
- `status` — **the skill's loop predicate**. One of:
  - `pending` — `decision` is set and waiting for an answer. Render via `AskUserQuestion` and write the answer back.
  - `answered` — the skill body wrote `answer` and the driver hasn't been re-invoked yet. (You'll only see this transiently, written by the skill body.)
  - `completed` — the generator returned (StopIteration). `result` holds the return value (the refine analysis path for slice-1 runs; the plan-document path for slice-2 runs that walked through both phases). Skill loop exits cleanly.
  - `aborted` — the operator chose an abort-style answer (`abort` / `stop` / `cancel`). Skill loop exits cleanly.
  - `error` — the driver hit an internal error. `error` holds the diagnostic. Driver exited 1.
- `result` — generator return value when `status == "completed"`. For a refine-only run this is the analysis path; for a run that walked through both refine and plan, the value depends on how the operator answered the plan HITL gate (e.g. the plan document path on `approve`).
- `error` — diagnostic message when `status == "error"`.
- `answer_log` — the operator's accumulated answer history. The driver replays this list on every invocation (see "Generator state across invocations" below); the slice-3 daemon variant inherits this field unchanged.

**The full 9-field envelope is a stable cross-bridge contract.** The slice-3 daemon variant in `orchestrator/substrate/claude_code/hitl_daemon.py` (TASK-3-2) consumes every field; do not drop or rename any field without bumping `version`.

#### Generator state across invocations (replay semantics)

Each `python3 bin/run_pipeline.py` invocation is a fresh process — generator frames cannot persist across processes. To resume at the right yield boundary across invocations, the driver **replays** the operator's answers from `answer_log` on every call: it spawns a fresh `run_pipeline_in_process(...)` generator, calls `next()` to land on the first yield, then loops `generator.send(replay)` over each historical answer to fast-forward to the next un-answered yield. This works because the generator is deterministic — the same `(pipeline_id, repo, issue_number, issue_body)` inputs combined with the same answer sequence reach the same yield boundary every time.

Practical consequence: **side effects (refiner subagent dispatch, worktree create / teardown, artifact write) re-run on every invocation.** For the walking-skeleton refine + plan phases this is acceptable (each subagent's worktree is idempotent and the artifact write overwrites). The implement phase has too many concurrent yields for replay to be practical, which is why slice 3 ships the daemon variant for implement-phase concurrency instead.

**Cost note.** Each re-spawn is a real Anthropic API call: tokens, plus 10–60 s of wall-clock per subagent. For slice 1's 2-yield refine phase this means the refiner (and both refine reviewers, when the target is `jwbron/egg`) spawn **twice** — once when the operator first sees the refine-gate, again when they answer it. Slice 2's plan phase (4 yields, per the ADR) compounds: stage B = 2 spawns, stage C = 4, stage D = 6, stage E = 8 — eight spawns just to reach the plan-gate's final yield. For a real `jwbron/egg` issue this is on the order of tens of dollars in Anthropic API spend per pipeline run before the slice-3 daemon variant lands and eliminates replay. If cost matters to your run, prefer the k3s substrate (no replay) until slice 3 lands; the cost cap (`EGG_PIPELINE_MAX_AGENT_INVOCATIONS`, slice 5) does not apply to this substrate until then.

#### The skill loop

Run the driver, read `status` and `decision`, render via `AskUserQuestion`, write the answer back to `pending_hitl.answer` (and bump `status` to `answered`), re-invoke the driver. Loop until `status ∈ {completed, aborted, error}`:

```bash
ISSUE=1234
REPO="jwbron/egg"
PIPELINE_ID="issue-${ISSUE}"
CONTRACT_PATH=".egg-state/contracts/${PIPELINE_ID}.json"

# Iteration N — ask the orchestrator for the next decision (positional
# pipeline_id; --repo / --issue-number flags match the driver's argparse
# at plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py:355-402).
python3 plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py \
    "${PIPELINE_ID}" \
    --repo "${REPO}" \
    --issue-number "${ISSUE}"

# Read status + decision out of the contract.
STATUS=$(python3 -c "import json,sys; e=json.load(open('${CONTRACT_PATH}'))['pending_hitl']; print(e['status'])")

case "${STATUS}" in
  pending)
    # Read pending_hitl.decision and render via AskUserQuestion (an
    # LLM-side tool — outside Bash). The skill body collects the
    # operator's selection into shell variable ${ANSWER}.
    # Then write the answer back to the envelope via the helper script.
    # The helper reads the JSON-encoded answer from stdin (so shell
    # quoting cannot mis-encode it), uses datetime.now(UTC) — matching
    # the driver's own _now_iso() at run_pipeline.py:103 — and writes
    # the contract atomically via tmp + os.replace (matching
    # _write_contract at run_pipeline.py:139-147). Failure to ferry
    # the answer exits non-zero so the skill loop notices.
    printf '%s' "${ANSWER}" | python3 -c '
import json, sys
sys.stdout.write(json.dumps(sys.stdin.read()))
' | python3 plugins/egg-sdlc/skills/egg-sdlc/bin/write_answer.py \
    --pipeline-id "${PIPELINE_ID}" \
    --state-root "$(dirname "$(dirname "${CONTRACT_PATH}")")" \
    --answer-stdin
    # Loop: re-invoke run_pipeline.py with the same args. The driver
    # promotes pending_hitl.answer → answer_log, clears answer to null,
    # replays the full answer_log into a fresh generator, and writes
    # the next pending_hitl.decision.
    ;;
  completed|aborted)
    # Read pending_hitl.result for the artifact path (completed) or the
    # abort diagnostic (aborted). Skill exits cleanly.
    ;;
  error)
    # Read pending_hitl.error for the diagnostic. Driver exited 1.
    ;;
esac
```

The skill body's `allowed-tools` frontmatter scopes `python3` to `plugins/egg-sdlc/skills/egg-sdlc/bin/*` so the skill cannot be coerced (via a prompt-injected issue body, say) into running arbitrary `python3 -c "..."` snippets. The two helpers under `bin/` (`run_pipeline.py` and `write_answer.py`) are the entire Python surface the skill can invoke; both ship in this PR and are read-reviewable next to `SKILL.md`. No separate `Write` permission is needed — `write_answer.py` is the only path that writes `pending_hitl.answer`, and it consumes the operator's selection from stdin so shell quoting cannot mis-encode it.

While the generator is paused at a yield boundary inside a single `bin/run_pipeline.py` invocation, the orchestrator's background threads (heartbeat poll, BRC re-review, message-bus tick) keep running so a long-paused HITL does not cause stuck-phase-transition alerts within that invocation. Dropping the generator (process exit) joins the background threads cleanly via `GeneratorExit` — no leaked threads across the skill→Python boundary.

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

> **Open question for slice-5 sequencing.** The slice-1 R2 verdict file (`r2-verdict.json`) records *only* the hook-logic half of R2 — it is **not** a green-light for the R15 model-(b) migration on its own. Before slice 5 reads the verdict as "ship Agent-tool dispatch," an empirical Claude-Code-side test must land that exercises real nested Agent-tool dispatch and observes `EGG_AGENT_ROLE` propagation in the child. Slice 5's R15 task should treat the verdict file as a necessary-but-not-sufficient input. Tracked in the slice-5 plan; this caveat is duplicated in `integration_tests/regression/test_pretooluse_hook_nested.py`'s module docstring so a reader of either surface sees the same constraint.

### Plan phase (landed in slice 2 of #2717)

The plan stage runs **three producers reviewed by one reviewer**:

| Role | When it spawns | Output |
|---|---|---|
| `architect` | First, solo | `.egg-state/agent-outputs/<issue>-architect-output.json` — approach summary + key design decisions + ordering constraints |
| `task_planner` | Concurrently with `risk_analyst`, downstream of the architect | `.egg-state/drafts/<issue>-plan.md` + `.egg-state/agent-outputs/<issue>-task_planner-output.json` — slice DAG with role-typed tasks |
| `risk_analyst` | Concurrently with `task_planner` | `.egg-state/agent-outputs/<issue>-risk_analyst-output.json` — risks with evidence, top-3, blocking concerns |
| `reviewer_plan` | After each producer's `CONSENSUS_PROPOSE` | `.egg-state/agent-outputs/<issue>-reviewer_plan-output.json` — ACK / NACK per producer edge |

Each role's rubric lives at `agents/<role>.md` and is prepended to the per-task prompt by `build_system_prompt(sources)`. The plan stage advances through three BRC edges (`architect → reviewer_plan`, `task_planner → reviewer_plan`, `risk_analyst → reviewer_plan`); the orchestrator's open-NACK barrier applies per edge.

**Plan HITL gate.** Once `CONSENSUS_CONFIRMED` fires on all three producer edges, the stage yields a plan-gate `HITLDecision` with these standard options:

- `approve` — advance to the implement phase (currently fenced until slice 3 of the #2717 rollout).
- `request_changes` — feed change requests back into a fresh plan cycle (each producer + the reviewer re-spawn with the operator's notes as a NACK-equivalent revision instruction).
- `change_approach` — kick the pipeline back to the refine phase so the refiner can re-research before another plan attempt.
- `stop` — abort the run; the skill loop exits with `pending_hitl.status = aborted`.

The skill surfaces the architect's `approach_summary`, the task_planner's slice DAG shape, the risk_analyst's top-3 risks + blocking concerns, and each per-edge `reviewer_plan` verdict alongside the decision so the operator decides with the full plan-team context in view.

### What's NOT in this skill (yet)

A non-exhaustive list of capabilities that the substrate-swap rollout targets but slices 1 + 2 of #2717 have not landed:

- **Implement / pr phases.** Slice 3 lands the implement-phase substrate (3 producers + 5 reviewers) + daemon HITL bridge; slice 4 lands the pr-phase substrate + the rest of the conformance matrix. If you advance past the plan HITL gate today, the skill raises `NotImplementedError` with a pointer to the active slice.
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
- **`NotImplementedError: claude-code substrate runs refine + plan only`**: you tried to advance past the plan HITL gate. Slices 1 + 2 of the #2717 rollout cover refine + plan; implement / pr land in slices 3 / 4 of the same rollout.
- **`NotImplementedError: EGG_SUBSTRATE=k3s requires the HTTP daemon`** (raised from `run_pipeline_in_process`): you set `EGG_SUBSTRATE=k3s` while running this in-process skill. k3s users use `orchestrator/cli.py:83 cmd_serve`, not the skill.
- **PreToolUse hook denies a write the role *should* be allowed**: the `settings.template.json` is wired against a stale or wrong `EGG_AGENT_ROLE`. The hook prints which role it saw — re-check the spawn env.
- **HITL takes a long time and you see no progress**: the orchestrator's background threads keep running inside each `bin/run_pipeline.py` invocation while the generator is paused on a yield; the heartbeat-during-HITL acceptance criterion guarantees this within an invocation. Between invocations (i.e. while the skill is rendering `AskUserQuestion` and waiting on the operator), the Python process has exited and the orchestrator state lives only in `.egg-state/contracts/<id>.json#pending_hitl`. If you genuinely want to abandon the run, close the session; the next invocation of `bin/run_pipeline.py` will resume from the contract file, or you can delete the contract file to discard the run entirely.

- **`pending_hitl.status` is `completed`, `aborted`, or `error`** after a `bin/run_pipeline.py` invocation: the loop is done. `completed` → read `pending_hitl.result` for the artifact path. `aborted` → the operator chose an abort-style answer; `pending_hitl.result` holds the abort diagnostic. `error` → read `pending_hitl.error` for the driver's diagnostic string; the driver exited 1. The skill loop should exit in all three cases, not call `bin/run_pipeline.py` again.

## Where this fits

- [Substrate-swap ADR](../../../../docs/architecture/claude-code-substrate.md) — the canonical reference for the four interfaces, the implementations, the eleven cq decisions, and the deferred-vs-landed status of each rollout item.
- [`plugins/refine-plan/`](../../../refine-plan/) — the earlier Markdown-only approximation of egg's refine + plan phases. This skill **supersedes** that for solo-developer use of the real orchestrator; `refine-plan` remains as a portable Python-deps-free alternative.
- [Concurrent execution guide](../../../../docs/guides/concurrent-execution.md) — the BRC protocol the substrate preserves.
- [Integration-test trust boundary](../../../../docs/architecture/integration-test-trust-boundary.md) — names "in-parent-Claude-Code-session" as a new trust context (R1).
