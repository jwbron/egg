<!-- Shared contract verification criteria: consumed by GHA prompt scripts AND orchestrator pipelines.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

## Default Contract Verification Rules

### Task Verification

For each task in the contract, verify:

1. **Implementation exists**: The described functionality is present in the code
2. **Acceptance criteria met**: The specific acceptance criteria for the task is satisfied
3. **Commit linked**: If a commit is linked, verify it relates to the task
4. **Tests present**: Where applicable, tests cover the new functionality
5. **Task record is complete**: The task's recorded `status` is `complete` with
   a linked commit. A task whose work appears in the diff but whose record is
   still `pending` is NOT verified — either the work landed unrecorded (the
   producer must mark it complete) or the record is the truth and the work is
   missing. Read the **live** task records (via the contract MCP/CLI surface),
   not the `.egg-state/contracts/` file in your checkout — that file is an
   init-time snapshot and does not reflect mutations.

### Scope: every producer, every row

Contract review covers **all task rows owned by the producer under review**,
not just the rows that overlap the proposal's diff. A proposal that delivers a
subset of the producer's rows and defers the rest ("will land in later
proposals") leaves the contract unsatisfied: the deferred rows are open
obligations. Do not approve the producer until every row it owns is delivered
and recorded complete, or a human has explicitly descoped the row.

### Phase Consistency

Check that:
- All tasks in completed phases are actually implemented
- Phase status matches task completion state
- No orphaned code exists that isn't covered by any task

### Acceptance Criteria Verification

For each acceptance criterion in the contract:
1. Read the criterion description
2. Examine the implementation to verify it meets the criterion
3. Note any gaps in your review

### Contract Integrity

Verify:
- No implementation changes violate previously verified criteria
- New changes don't break existing contract compliance
- All required files listed in tasks are present

## Verification Ladder — CONFIRMED / PLAUSIBLE / REFUTED

This lens shares the verification discipline defined in
[`code-review-criteria.md`](./code-review-criteria.md). Before a gap becomes a
finding, assign it exactly one verdict; the evidence duties are **symmetric** —
quote the task row or code line that justifies the call whether you keep the
finding or drop it.

- **CONFIRMED** — you can name the unmet obligation concretely: the task row,
  the acceptance criterion it fails, and the missing or contradicting code.
  Quote the live task record and the diff (or its absence).
- **PLAUSIBLE** — the obligation looks unmet but the evidence is incomplete
  (the work may live in a file you have not read, or the task record may lag
  the diff). State what would confirm it. **PLAUSIBLE by default** — do not
  refute for being uncertain.
- **REFUTED** — the criterion is actually met, or the row is explicitly
  descoped by a human. Quote the implementing line or the descope decision.

Two companion rules:

1. **Blocking must reproduce.** A finding may be `blocking` only if it is
   CONFIRMED with a named unmet row/criterion. A suspicion you cannot tie to a
   specific obligation cannot block; carry it as advisory.
2. **Drop only the refuted; downgrade the unconfirmed.** Discard REFUTED
   candidates; downgrade PLAUSIBLE ones to advisory (non-blocking) so the
   signal survives without burning a producer revision round.

**Scratch checks (read-only).** You may run read-only commands to confirm a
candidate — read the **live** task records via the contract MCP/CLI surface
(not the init-time `.egg-state/contracts/` snapshot), Grep for the
implementing symbol, `git show` the linked commit. Do **not** mutate the
working tree, push, or run destructive commands.
