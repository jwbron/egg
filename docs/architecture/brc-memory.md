# BRC Memory Artifact

> Per-role-per-pipeline distilled memory file written by reviewers during BRC
> consensus so a stateless event-pump handler can re-enter a review cycle with
> continuity — without re-reading the codebase, the change, or the prior
> conversation. The artifact exists because the orchestrator owns the BRC wait
> and spawns each agent one-shot per actionable event
> ([#2908](https://github.com/jwbron/egg/issues/2908),
> [#3164](https://github.com/jwbron/egg/issues/3164)): a one-shot handler has no
> in-process memory across invocations, so the reviewer's distilled
> understanding has to live somewhere durable to the pod.

## Status & lifetime

The memory artifact is **ephemeral coordination state, not durable audit
material**. Recovery does NOT depend on the file surviving — the durable
backstop is the orchestrator message history rehydrated by
`reconstruct_tracker_from_messages`. The file lives next to the agent that
wrote it for the lifetime of the pod and may be cleaned up with it. Treat the
artifact as ephemeral regardless of any "durable" framing in older design
notes.

## What problem this solves

The orchestrator owns the BRC wait and spawns each reviewer agent one-shot per
actionable event; there is no long-running in-pod session holding a blocking
wait between events. A stateless one-shot has no working memory across
invocations, so something else has to carry the reviewer's distilled
understanding of the codebase, the change under review, and the verdicts
already issued on each producer. That something is this file.

The **writer** lives in `brc_ack` / `brc_nack`, gated by `EGG_BRC_MEMORY`. The
**reader** is `compose_event_prompt`, which consumes the per-producer
`last_reviewed_commit_sha` to parameterise an adversarial git-log delta on
re-entry. Both writer and reader are active by default in production.

## File path

```
.egg-state/agent-outputs/<role>/brc-memory-<pipeline-id>.md
```

- `<role>` is the writer's `EGG_AGENT_ROLE` (e.g. `reviewer_code`,
  `reviewer_contract`). Each role gets its own subdirectory so two roles never
  collide on a shared file.
- `<pipeline-id>` is `EGG_PIPELINE_ID` (fallback: `issue-<EGG_ISSUE_NUMBER>`) —
  see "Scope key" below.
- The base directory is resolved against `EGG_REPO_PATH`.
- The subdirectory is created if absent on first write.

### Scope key

Memory is scoped per `(pipeline, role, slice_id, phase)` — a reviewer working
on one slice of the implement phase keeps a separate memory entry from the same
reviewer working on another slice or on a different phase. Scope keys keep
parallel slice teams from clobbering each other's distilled state, and keep one
pipeline's distilled state out of the next pipeline's prompts.

The path carries `<role>` and `<pipeline-id>`; the `(slice_id, phase)` half of
the scope key is supplied by the agent pod's surrounding context. Per-slice
agents run in per-slice worktrees with slice-scoped `EGG_REPO_PATH` and
`EGG_SLICE_ID` env vars (see [Slice-DAG Implement Phase](slice-dag.md)), so two
reviewers working on different slices each resolve the memory path against a
different repo root. The schema also embeds `producer` per-assessment
subsection so a reviewer reading the file can locate the specific producer
entry within the current pod's scope.

The pipeline dimension cannot ride on worktree isolation the way slices do: the
memory file is committed to the pipeline branch, merges to main with the
context PR, and then seeds **every later pipeline's** fresh worktree at the same
role-keyed path. Without the pipeline-id in the filename, a fresh pipeline's
first event prompt would carry a prior pipeline's memory as its authoritative
"distilled state" section
([#3163](https://github.com/jwbron/egg/issues/3163)). The pipeline-id filename
suffix makes prior-pipeline memory files inert (nothing resolves the unsuffixed
`brc-memory.md` name) without needing a migration of files already on main.

### Fail-closed path constructor

The path constructor **raises before any directory or file is created** if
`EGG_AGENT_ROLE` is unset or empty, or if neither `EGG_PIPELINE_ID` nor
`EGG_ISSUE_NUMBER` resolves a pipeline id. This is fail-closed by design: the
writer must never fall through to a degenerate
`.egg-state/agent-outputs//brc-memory.md` path where two roles could collide on
a shared file — or to a cross-pipeline shared file — and a silent failure on
write is unacceptable for a primitive that other reviewers consult to make
veto-bearing decisions. The orchestrator-side reader
(`orchestrator/routes/event_prompt.py`) is fail-soft instead: an unresolvable
pipeline id omits the memory section rather than failing the composer.

## Schema

The memory file is markdown with three top-level sections. Six fields are
required; a seventh (`enrichment_sha`) is synthesized by the renderer from
`last_reviewed_commit_sha` and is rendered immediately before
`summary_of_assessment` so consumers can detect stale claims without parsing
two separate fields:

```markdown
## Codebase / change model

<!-- enrichment (claims, not ground truth); re-verify vs the live git-log delta -->
<distilled prose: what the codebase does, what this slice changes,
which subsystems are in play>

## Per-producer assessment

<!-- summaries are SHA-stamped claims; stale when enrichment_sha != the producer's current proposal SHA -->

### <producer-role>

- producer: <role>
- last_reviewed_commit_sha: <40-char SHA of HEAD at review time>
- prior_verdict: <ACK | NACK | conditional-ACK>
- prior_nack_reasons: <bulleted reasons from the last NACK, if any>
- prior_conditional_obligation: <the pre_merge_condition text, if any>
- enrichment_sha: <SHA the summary_of_assessment was authored against;
  equals last_reviewed_commit_sha by construction>
- summary_of_assessment: <distilled prose of what was reviewed and why
  the verdict landed where it did>

### <next-producer-role>
...

## Decision log

- <ISO timestamp> <verb> <subject>: <one-line decision>
- ... (capped at the last 20 entries)
```

### Field semantics

| Section | Field | Purpose |
|---------|-------|---------|
| `## Codebase / change model` | (prose) | Distilled understanding of the change set so a fresh handler doesn't re-read the codebase end-to-end. |
| `## Per-producer assessment` | `producer` | The role of the producer being assessed. |
| | `last_reviewed_commit_sha` | SHA of `HEAD` at review time. The reader substitutes this into `git log {sha}..HEAD --not origin/{base_branch} -p` to drive an adversarial re-review delta on the next re-proposal. |
| | `prior_verdict` | One of `ACK`, `NACK`, or `conditional-ACK` so a re-entered handler knows which path to take. |
| | `prior_nack_reasons` | Carries forward NACK reasons so re-review can verify they were addressed. Cleared on the next ACK from this reviewer: the producer's fixes have, by the ACK contract, resolved the NACKs that ACK replaces, so a reader inspecting this field after an ACK sees `[]`. The decision-log entry for the NACK remains for the audit trail. |
| | `prior_conditional_obligation` | Echoes any `pre_merge_condition` attached to a conditional ACK ([#1998](https://github.com/jwbron/egg/issues/1998) / [#2336](https://github.com/jwbron/egg/issues/2336)). |
| | `enrichment_sha` | SHA the `summary_of_assessment` was authored against; synthesized by the renderer from `last_reviewed_commit_sha` (always equal by construction). Consumers call `egg_agent.queryable_env.enrichment_is_stale(enrichment_sha, current_proposal_sha)` to decide whether to trust the summary or re-derive it from the live `git log` delta. |
| | `summary_of_assessment` | Distilled rationale — the bit that takes the most context to reconstruct from raw transcript. This is agent-authored enrichment (claims, not ground truth); compare `enrichment_sha` against the producer's current proposal SHA before trusting it. |
| `## Decision log` | (entries) | Append-only narrative of significant moments in the review, **capped at the last 20** via distill-on-write. |

### Decision-log cap (distill-on-write)

The decision log is capped at the **last 20 entries** on every write. An
unbounded log eventually pushes the memory file out of the cacheable prefix of
a per-event handler invocation — the very cost the artifact exists to avoid.
Distilling on write keeps the file bounded without requiring a separate
compaction step.

## Modes — `EGG_BRC_MEMORY`

The writer is gated by `EGG_BRC_MEMORY`, which takes one of three values:

| Mode | Writes | Reads | Notes |
|------|--------|-------|-------|
| `full` | yes | yes | **Default.** Handlers populate the file and the event-pump consults it on re-entry. |
| `write-only` | yes | no | Handlers populate the file but no other code path reads it. Available as a one-release rollback of the reader. |
| `off` | no | no | One-release rollback escape hatch; writes are no-ops. |

When the env var is unset, the mode is `full`. Operators that need to roll back
the reader for one release can set `EGG_BRC_MEMORY=write-only` explicitly.

## Atomic-write contract

Writes go through an atomic tempfile + `os.replace` helper so that two
back-to-back invocations from the same handler — or a crash mid-write — never
expose a partial file. The shared helper guarantees no within-pod partial
writes.

The writer reuses an existing helper:

- `shared/egg_overseer/state.py:266` — the `save_agent_timing` body, with the
  canonical shape `tempfile.NamedTemporaryFile(delete=False, dir=parent) →
  write → flush → os.fsync → os.replace`, guarded by `_file_lock`.

The acceptance criterion is testable independently of the helper chosen:
back-to-back handler invocations under fault injection must never observe a
partial-state file.

## Role-allowlist coverage

The memory file lives in `.egg-state/agent-outputs/<role>/`, which is in
**every participant role's** write-allowlist. The trailing-slash pattern
matches as a recursive prefix in `match_pattern`
(`shared/egg_restrictions/matchers.py:33`), so the per-role subdirectory needs
no separate carve-out.

| Role | Pattern source | Line |
|------|----------------|------|
| `coder` | `CODER_PATTERNS.block_exempt_patterns` | `shared/egg_restrictions/patterns.py:231` |
| `tester` | `_build_tester_pattern.allowed_patterns` | `shared/egg_restrictions/patterns.py:277` |
| `documenter` | `_build_documenter_pattern.allowed_patterns` | `shared/egg_restrictions/patterns.py:307` |
| `architect` | `ARCHITECT_PATTERNS.allowed_patterns` | `shared/egg_restrictions/patterns.py:367` |
| `task_planner` | `TASK_PLANNER_PATTERNS.allowed_patterns` | `shared/egg_restrictions/patterns.py:377` |
| `risk_analyst` | `RISK_ANALYST_PATTERNS.allowed_patterns` | `shared/egg_restrictions/patterns.py:387` |
| `applier` | `APPLIER_PATTERNS.allowed_patterns` | `shared/egg_restrictions/patterns.py:401` |
| Reviewers (`reviewer_code`, `reviewer_code_holistic`, `reviewer_agent_design`, `reviewer_refine`, `reviewer_plan`, `reviewer_security`, `reviewer_concurrency`) | `_REVIEWER_ALLOWED` (shared list) | `shared/egg_restrictions/patterns.py:416-419`, referenced by `REVIEWER_CODE_PATTERNS` at `:436`, `REVIEWER_PLAN_PATTERNS` at `:516`, and the rest of the reviewer block at `:443-535` |
| `reviewer_contract` | `_REVIEWER_CONTRACT_ALLOWED` (separate list because contract reviewer also writes `.egg-state/contracts/`) | `shared/egg_restrictions/patterns.py:452-456`, referenced by `REVIEWER_CONTRACT_PATTERNS` at `:472` |
| `refiner` | `REFINER_PATTERNS.allowed_patterns` | `shared/egg_restrictions/patterns.py:493` |
| `overseer` | `OVERSEER_PATTERNS.allowed_patterns` | `shared/egg_restrictions/patterns.py:546` |

The path also passes the in-sandbox `tool_interceptor.check_file_write_permission`
guard and — when the file is staged into a commit — the `validate_agent_push`
check (defined at `shared/egg_restrictions/checker.py:98`, invoked from
`gateway/phase_filter.py`). Neither requires a separate allowlist entry for the
`<role>/` subdirectory because the parent pattern already prefix-matches.

## Writer hook points

The writer is invoked from the two BRC review handlers:

- `sandbox/egg_agent_tools/handlers/brc.py:505` — `brc_ack`
- `sandbox/egg_agent_tools/handlers/brc.py:586` — `brc_nack`

Both handlers already carry the `reason` and `files_reviewed` fields that seed
the per-producer assessment and the decision log, so the memory write is
**action-scaffolded** off the ACK/NACK signal payload rather than free-form
journaling. Handler return values are unchanged in every mode — the writer is a
side-effect, not part of the orchestrator-bound contract.

## How the reader consumes it (inline path)

When `EGG_BRC_MEMORY=full`, the event-pump's `compose_event_prompt` step reads
the per-producer `last_reviewed_commit_sha` and parameterises an adversarial
re-review git delta:

```
git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p
```

This is intentionally the **full** delta since the prior review, not just the
orchestrator's `changed_artifacts` set — the re-review must audit every change
as a fresh review (the BRC adversarial re-review contract shared with the
PR-side reviewer — see
[`shared/prompts/REVIEWER-SYNC.md` Diff command (re-review / delta)](../../shared/prompts/REVIEWER-SYNC.md)),
or the stateless pump systematically weakens adversarial review.

When the mode is `write-only`, the read path passes an empty memory excerpt —
writes happen but reads are no-ops, preserving the inert default.

## JIT-pull path

`compose_event_prompt` accepts a `jit_pull` flag. When `jit_pull=True`:

- The delta and memory excerpt are **not inlined**. Instead,
  `compose_event_prompt` renders pointer blocks — the exact `git log` recipe
  scoped by the `last_reviewed_commit_sha` + proposal SHA anchors, plus
  `mcp__brc__read_peer_artifact` / `GET /<pipeline_id>/brc-transcript` handles —
  so only small pointers stay resident in the protected root.
- The `enrichment_sha` in each producer's assessment block lets the agent
  detect whether the in-memory summary is still valid for the current proposal
  before deciding to pull the full delta. Call
  `egg_agent.queryable_env.enrichment_is_stale(enrichment_sha, current_proposal_sha)`
  to determine whether to trust the summary or re-derive it from the live
  `git log` delta.
- **Honest limit**: pulling the delta/transcript does NOT bound the context
  window. A pulled slice stays resident until the next reseed. The reseed
  bounds the window; the pull only lowers the resident root cost and makes the
  reseed re-pull-able.

The canonical module for the JIT-pull renderers is `shared/egg_agent/queryable_env.py`.

For the full architecture of the reader (composer shape, 10 KB envelope,
tail-position memory delivery, preamble collapse, wrapper interplay), see
[Orchestrator — BRC Per-Event Prompt Composer + Preamble Collapse](orchestrator.md#brc-per-event-prompt-composer--preamble-collapse)
and its wait-side companion
[agent-wait-patterns §10.9](../reference/agent-wait-patterns.md#109-brc-per-event-prompt-composer--preamble-collapse).

## Writer guarantees

The writer holds these guarantees:

- `brc_ack` and `brc_nack` calls produce a well-formed memory file with all six
  schema fields populated.
- Decision-log entries are capped at 20 via distill-on-write.
- The atomic-write contract holds — back-to-back handler invocations never see
  a partial state (asserted via fault injection in the writer tests).
- The path constructor raises on empty `EGG_AGENT_ROLE` (or an unresolvable
  pipeline id) **before** creating any file or directory.
- `EGG_BRC_MEMORY=off` produces no file (the one-release rollback escape hatch).
- The `.egg-state/agent-outputs/<role>/` subdirectory is created if absent on
  first write.
- Handler return values are unchanged for callers in every case.

## See also

- [Concurrent Execution](../guides/concurrent-execution.md) — canonical BRC
  protocol reference (consensus wrapper, action guards, peer consensus).
- [Slice-DAG Implement Phase](slice-dag.md) — how per-slice BRC trackers scope
  the memory file's `slice_id` key.
- [Agent Roles](../reference/agent-roles.md) — which roles participate in BRC
  and therefore write or read this file.
- [Issue #2908](https://github.com/jwbron/egg/issues/2908) — the durable fix
  for the agent-held-wait fall-out (#2906) that this artifact is part of.
