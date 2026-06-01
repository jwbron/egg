# BRC Memory Artifact

> Per-role-per-pipeline distilled memory file written by reviewers during BRC
> consensus so a future stateless event-pump handler can re-enter a review
> cycle with continuity — without re-reading the codebase, the change, or the
> prior conversation. Introduced by [#2908](https://github.com/jwbron/egg/issues/2908),
> slice-1.

## Status & lifetime

The memory artifact is **ephemeral coordination state, not durable audit
material**. Recovery does NOT depend on the file surviving — the durable
backstop is the orchestrator message history rehydrated by
`reconstruct_tracker_from_messages`. The file lives next to the agent that
wrote it for the lifetime of the pod and may be cleaned up with it.

This was an explicit operator correction to the slice-1 plan
(`.egg-state/drafts/issue-2908-impl2-plan.md`, HITL #3): treat the artifact
as ephemeral, regardless of any "durable" framing in older design notes.

## What problem this solves

A consensus reviewer's lifecycle today is a long-running session that holds
a blocking `egg-orch message wait-loop` between events. Issue #2908 is
removing that seam: a wrapper will hold the wait, and an agent will be
invoked one-shot per actionable event. A stateless one-shot has no working
memory across invocations, so something else has to carry the reviewer's
distilled understanding of the codebase, the change under review, and the
verdicts already issued on each producer. That something is this file.

Slice-1 lands the **writer** in `brc_ack` / `brc_nack` and gates everything
behind `EGG_BRC_MEMORY` so production pipelines stay inert. The **reader**
(`compose_event_prompt` consuming the per-producer `last_reviewed_commit_sha`
to parameterise an adversarial git-log delta) lands in slice-3.

## File path

```
.egg-state/agent-outputs/<role>/brc-memory.md
```

- `<role>` is the writer's `EGG_AGENT_ROLE` (e.g. `reviewer_code`,
  `reviewer_contract`). The subdirectory layout is per architect od-1 of the
  slice-1 design.
- The base directory is resolved against `EGG_REPO_PATH`.
- The subdirectory is created if absent on first write.

### Scope key

Memory is scoped per `(role, slice_id, phase)` — a reviewer working on
slice-1 of the implement phase keeps a separate memory entry from the same
reviewer working on slice-2 or on a different phase. Scope keys keep
parallel slice teams from clobbering each other's distilled state.

The path itself only carries `<role>`; the `(slice_id, phase)` half of the
scope key is supplied by the agent pod's surrounding context. Per-slice
agents run in per-slice worktrees with slice-scoped `EGG_REPO_PATH` and
`EGG_SLICE_ID` env vars (see [Slice-DAG Implement Phase](slice-dag.md)),
so two reviewers working on different slices each resolve
`.egg-state/agent-outputs/<role>/brc-memory.md` against a different repo
root. The schema also embeds `producer` per-assessment subsection so a
reviewer reading the file can locate the specific producer entry within
the current pod's scope. The exact encoding of the `phase` dimension within
the file is finalised by the slice-1 writer (task-1-6) and this doc will
follow whatever the implementation lands.

### Fail-closed path constructor

The path constructor **raises before any directory or file is created** if
`EGG_AGENT_ROLE` is unset or empty. This is fail-closed by design (architect
od-1 + risk_analyst R14): the writer must never fall through to a degenerate
`.egg-state/agent-outputs//brc-memory.md` path where two roles could collide
on a shared file, and silent-fail-on-write is unacceptable for a primitive
that other reviewers will eventually consult to make veto-bearing decisions.

## Schema

The memory file is markdown with three top-level sections. The six required
fields are reproduced verbatim from architect v2
`design.memory_schema.required_fields`:

```markdown
## Codebase / change model

<distilled prose: what the codebase does, what this slice changes,
which subsystems are in play>

## Per-producer assessment

### <producer-role>

- producer: <role>
- last_reviewed_commit_sha: <40-char SHA of HEAD at review time>
- prior_verdict: <ACK | NACK | conditional-ACK>
- prior_nack_reasons: <bulleted reasons from the last NACK, if any>
- prior_conditional_obligation: <the pre_merge_condition text, if any>
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
| | `last_reviewed_commit_sha` | SHA of `HEAD` at review time. **Slice-3 reads this** to run `git log {sha}..HEAD --not origin/{base_branch} -p` for an adversarial re-review delta on the next re-proposal. |
| | `prior_verdict` | One of `ACK`, `NACK`, or `conditional-ACK` so a re-entered handler knows which path to take. |
| | `prior_nack_reasons` | Carries forward NACK reasons so re-review can verify they were addressed. |
| | `prior_conditional_obligation` | Echoes any `pre_merge_condition` attached to a conditional ACK (#1998 / #2336). |
| | `summary_of_assessment` | Distilled rationale — the bit that takes the most context to reconstruct from raw transcript. |
| `## Decision log` | (entries) | Append-only narrative of significant moments in the review, **capped at the last 20** via distill-on-write. |

### Decision-log cap (distill-on-write)

The decision log is capped at the **last 20 entries** on every write. The
rationale (architect od-2) is that an unbounded log eventually pushes the
memory file out of the cacheable prefix of a per-event handler invocation —
the very cost the artifact exists to avoid. Distilling on write keeps the
file bounded without requiring a separate compaction step.

## Modes — `EGG_BRC_MEMORY`

The writer is gated by `EGG_BRC_MEMORY`, which takes one of three values:

| Mode | Writes | Reads | Notes |
|------|--------|-------|-------|
| `off` | no | no | **Default.** Slice-1 ships inert in production. |
| `write-only` | yes | no | Slice-1 rollout posture: handlers populate the file but no other code path reads it. The read path lands in slice-3. |
| `full` | yes | yes | Slice-3+ end state: handlers populate the file and the event-pump consults it on re-entry. |

The default of `off` ensures slice-1 produces no observable behavior change
in production. Slice-4 is responsible for flipping the rollout dial.

## Atomic-write contract

Writes go through an atomic tempfile + `os.replace` helper so that two
back-to-back invocations from the same handler — or a crash mid-write —
never expose a partial file. The on-disk contract guarantees no within-pod
partial writes (v2 atomic-write contract).

Slice-1 **inlines** the atomic-write body in
`sandbox/egg_agent_tools/handlers/brc_memory.py::write_memory_atomic`
rather than reusing a shared helper. The pattern mirrors
`shared/egg_contracts/usage_loader.py:95` (`_atomic_write`):
`tempfile.mkstemp(dir=parent)` → write → `flush` → `os.fsync` →
`os.chmod(0o644)` → `os.replace` → best-effort `os.unlink` of the tempfile
on any exception. The architect note (od-2 / slice-1 plan) accepted the
inline form because the body is ~20 lines, lifting it through `shared/`
would expand the slice's review surface for negligible reuse, and the
neighbouring `shared/egg_overseer/state.py::save_agent_timing` shape
(`NamedTemporaryFile`-based, flock-protected) is heavier than the writer
actually needs.

The acceptance criterion is testable independently of the helper chosen:
back-to-back handler invocations under fault injection must never observe a
partial-state file (slice-1 task-1-9).

## Role-allowlist coverage

The memory file lives in `.egg-state/agent-outputs/<role>/`, which is in
**every participant role's** write-allowlist. The trailing-slash pattern
matches as a recursive prefix in `match_pattern`
(`shared/egg_restrictions/matchers.py:33`), so the per-role subdirectory
needs no separate carve-out.

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
guard and — when the file is staged into a commit — the
`validate_agent_push` check (defined at
`shared/egg_restrictions/checker.py:98`, invoked from
`gateway/phase_filter.py`). Neither requires a separate allowlist entry
for the `<role>/` subdirectory because the parent pattern already
prefix-matches.

## Writer hook points

The writer is invoked from the two BRC review handlers:

- `sandbox/egg_agent_tools/handlers/brc.py:576` — `brc_ack` (writer call
  at `:661`)
- `sandbox/egg_agent_tools/handlers/brc.py:673` — `brc_nack` (writer call
  at `:728`)

Both handlers already carry the `reason` and `files_reviewed` fields that
seed the per-producer assessment and the decision log, so the memory
write is **action-scaffolded** off the ACK/NACK signal payload rather than
free-form journaling. Handler return values are unchanged in every mode —
the writer is a side-effect, not part of the orchestrator-bound contract.
Writer failures are caught at the handler boundary and logged (never
re-raised), so a memory regression cannot break the BRC review path that
already succeeded at the orchestrator boundary.

### `last_reviewed_commit_sha` propagation

The per-producer `last_reviewed_commit_sha` field threads through three
layers — slice-3's adversarial-re-review delta depends on the SHA being
the producer's actual proposal head, not a guess:

1. **Orchestrator** (`orchestrator/peer_consensus.py`) — `handle_ack`
   and `handle_nack` surface the producer's `proposal_commit_sha` in the
   signal response payload. The capture lives next to the matrix mutation
   so a regression in propagation is catchable at the orchestrator
   boundary.
2. **Handler** (`sandbox/egg_agent_tools/handlers/brc.py`) —
   `_resolve_proposal_commit_sha` reads the SHA from the orchestrator
   response (or an explicit `req["commit_sha"]` override for test /
   replay paths). An absent SHA renders as `-` in the schema so the file
   is still well-formed.
3. **Writer** (`brc_memory.record_review`) — stamps the SHA into the
   per-producer `last_reviewed_commit_sha` block. Slice-3's
   `compose_event_prompt` reads this SHA to parameterise the
   `git log {sha}..HEAD --not origin/{base_branch} -p` adversarial
   re-review delta.

## How slice-3 reads it

Once `EGG_BRC_MEMORY=full` is in effect, the event-pump's
`compose_event_prompt` step reads the per-producer
`last_reviewed_commit_sha` and parameterises an adversarial re-review git
delta:

```
git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p
```

This is intentionally the **full** delta since the prior review, not just
the orchestrator's `changed_artifacts` set — the re-review must audit
every change as a fresh review (the BRC adversarial re-review contract),
or the stateless pump systematically weakens adversarial review
(risk_analyst R6).

When the mode is `write-only` (the slice-1 rollout posture), the read path
passes an empty memory excerpt — writes happen but reads are no-ops,
preserving the inert default.

## Server-side next-action route (slice-1, supporting)

Slice-1 also lands the orchestrator-side **next-action** route the
slice-2 event-pump wrapper drives between events. The route inspects the
in-memory `PeerConsensusTracker` (with a message-store replay fallback)
and returns a single deterministic action a role should take next —
`propose`, `ack`, `nack`, `confirm`, `complete`, or `wait` — together
with an action-specific `event_payload` and a human-readable `reason`.

| Surface | Path / verb |
|---------|-------------|
| REST | `POST /api/v1/pipelines/<pipeline_id>/consensus/next-action` |
| CLI | `egg-orch brc next-action --role <role> [--slice-id <id>] [--json]` |
| Source | `orchestrator/routes/consensus.py` |

The derivation lives in the orchestrator (not in wrapper bash) so the
sequencing logic is unit-testable and so producer / reviewer / dual-role
sub-cases share one code path. Slice-1 ships the route and the CLI
verb-level alias; slice-2 wires them into the wrapper. See
[Orchestrator CLI Reference — BRC Verb-Level Subcommands](../reference/orchestrator-cli.md#brc-verb-level-subcommands-2908-slice-1)
for the CLI surface.

The matching read/derive verb-level aliases land alongside the route as
shell-friendly wrappers around the existing handler layer:

- `egg-orch brc get-state` — verb-level alias for `mcp__brc__get_state`
  (returns the structured tracker snapshot the wrapper bash parses with
  `jq`).
- `egg-orch brc list-blocking` — verb-level alias for
  `mcp__brc__list_blocking` (newline-delimited by default for
  `while read role; do …; done`; `--json` returns the array).
- `egg-orch phase get-context` — verb-level alias for
  `mcp__phase__get_context`.

The write-side BRC verbs (`propose`, `ack`, `nack`, `withdraw`,
`confirmed`) keep their existing `egg-orch consensus …` parent so the
established CLI surface is preserved.

## Acceptance contract for the writer (slice-1)

The acceptance set codified by slice-1 task-1-6:

- `brc_ack` and `brc_nack` calls with `EGG_BRC_MEMORY=write-only` produce a
  well-formed memory file with all six schema fields populated per
  architect v2 `design.memory_schema`.
- Decision-log entries are capped at 20 via distill-on-write.
- Atomic-write contract holds — back-to-back handler invocations never see
  a partial state (asserted via fault injection in slice-1 tests).
- Path constructor raises on empty `EGG_AGENT_ROLE` **before** creating any
  file or directory.
- `EGG_BRC_MEMORY=off` produces no file (the default behavior in
  production).
- The `.egg-state/agent-outputs/<role>/` subdirectory is created if absent
  on first write.
- Handler return values are unchanged for callers in every case.

## Open-decision references

The slice-1 design preserves these open decisions for the wider issue
([#2908](https://github.com/jwbron/egg/issues/2908)) — they are recorded
here so reviewers can find them when they touch the surrounding subsystem:

- **od-1** — subdirectory layout (`.egg-state/agent-outputs/<role>/`) and
  the fail-closed path constructor.
- **od-2** — distill-on-write decision-log cap at 20 entries (vs.
  append-only-with-tail-breakpoint, which would need prompt-construction
  control `claude -p` does not currently expose).
- **R14** (risk_analyst) — the fail-closed path-constructor risk that
  motivates raising on empty `EGG_AGENT_ROLE`.

## See also

- [Concurrent Execution](../guides/concurrent-execution.md) — canonical BRC
  protocol reference (consensus wrapper, action guards, peer consensus).
- [Slice-DAG Implement Phase](slice-dag.md) — how per-slice BRC trackers
  scope the memory file's `slice_id` key.
- [Agent Roles](../reference/agent-roles.md) — which roles participate in
  BRC and therefore write or read this file.
- [Issue #2908](https://github.com/jwbron/egg/issues/2908) — the durable
  fix for the agent-held-wait fall-out (#2906), of which this artifact is
  slice-1.
