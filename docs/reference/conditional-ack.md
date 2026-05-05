# Conditional ACK Reference

A **conditional ACK** is a reviewer verdict variant introduced in [#1998](https://github.com/jwbron/egg/issues/1998): the work is approved, but a specific human-only action must be performed *at merge time* for the PR to be correct in production. The reviewer attaches the obligation to the ACK; the orchestrator persists it, surfaces it in status output, and renders it on the auto-created PR body so the merger cannot skim past it.

It is not a soft NACK. If the agents can address the concern themselves — by editing code, adding a test, or re-running a step — NACK with the reason. Conditional ACK is reserved for actions the sandbox cannot perform (e.g. a `git mv` blocked by the gateway, a cross-repo config flip, a manual datastore migration).

## When to use it

Use conditional ACK when **all** of these are true:

1. The diff under review is correct and complete.
2. Merging it as-is would be wrong *in production* without an additional step.
3. The additional step is one a human has to run — agents cannot push it through the gateway, or it touches a system outside the pipeline's reach.

If any of these fail, pick a different verdict:

| Situation | Verdict |
|-----------|---------|
| Diff is wrong or incomplete | **NACK** with a clear `--reason` |
| Diff is correct and safe to merge as-is | **ACK** (unconditional) |
| Diff is correct but needs a human step at merge time | **Conditional ACK** (this doc) |

## CLI invocation

```bash
egg-orch consensus ack <producer_role> <pipeline_id> \
  --files-reviewed <path> [<path> ...] \
  --reason "<substantive rationale>" \
  --pre-merge-condition "<the action the merger must perform>"
```

Example:

```bash
egg-orch consensus ack coder issue-42 \
  --files-reviewed orchestrator/routes/pipelines.py \
  --reason "Logic is correct; the PR body builder queries the live tracker safely" \
  --pre-merge-condition "git mv orchestrator/routes/legacy.py orchestrator/routes/archive/legacy.py before merge"
```

The same surface is available via the `mcp__brc__ack` MCP tool — pass `pre_merge_condition` alongside `artifact_references` and `reason`.

A conditional NACK is rejected at the schema layer — the combination is nonsensical because NACK already blocks the producer, leaving nothing to defer.

### Marking an obligation resolved within the PR (#2336)

Obligations sometimes get satisfied inside the same PR's diff after the initial conditional ACK landed — for example, a reviewer attaches "verify the tester committed the patch-path rewrite" and the tester subsequently lands that commit. Re-ACK with `--pre-merge-condition-resolved-in-diff <sha>` to record the resolving commit:

```bash
egg-orch consensus ack tester issue-42 \
  --files-reviewed tests/tools/_select_tests_helpers.py \
  --reason "Patch-path rewrite is correct" \
  --pre-merge-condition "Verify make test-all is green against the merged branch state" \
  --pre-merge-condition-resolved-in-diff 2c319626a
```

The PR-body renderer demotes resolved obligations from the merge-blocking `## ⚠️ Pre-merge Obligations` section to a `## ✅ Resolved within this PR` subsection, with a link to the satisfying commit. The merge-blocking banner only fires when at least one obligation is still open. The flag requires `--pre-merge-condition`; a resolution SHA on a plain ACK is rejected at the boundary.

## How obligations are tracked

- **Live tracker.** `record_ack` stores the condition on the approval-matrix edge for the current proposal version ([`orchestrator/approval_matrix.py`](../../orchestrator/approval_matrix.py)).
- **Version scoping.** `get_pre_merge_conditions()` returns only conditions attached to each producer's *current* proposal version. If a producer re-proposes, the reviewer's prior conditional ACK is dropped until they re-attach the obligation to the new version.
- **NACK clears it.** A NACK or `invalidate_ack` clears any condition (and any resolution SHA or in-cycle resolution flag) on that edge — there is no such thing as a lingering conditional NACK.
- **Resolution scoping.** A `--pre-merge-condition-resolved-in-diff` SHA travels with the obligation: it is cleared on NACK / re-propose / invalidate, and a re-ACK without the flag explicitly returns the obligation to the open list.
- **In-cycle resolution (#2338).** When the conditioning work lands on the producer's branch within the same pipeline run — e.g. the tester cherry-picks a commit the coder is gateway-blocked from — the satisfier calls `mcp__brc__resolve_obligation` to mark the obligation satisfied. `get_pre_merge_conditions()` filters out resolved entries so the PR body and HITL gate stop surfacing them. Resolution is per-version: any later `record_ack` / `record_nack` / `invalidate_ack` resets the resolved flag, so a re-attached obligation on a later proposal starts un-resolved.
- **Contract persistence.** When the human approves obligations via the HITL gate, they are written to `contract.pr.deferred_actions` (`PRMetadata.deferred_actions`, a `list[DeferredAction]`), so they survive tracker teardown between phase close and PR creation. Each entry carries `reviewer`, `condition`, and an optional `resolved_in_diff` SHA. Legacy `list[str]` entries from pre-#2336 contracts still load — they are coerced to `DeferredAction` with the reviewer parsed from the `<reviewer>: <condition>` prefix. The PR body renderer prefers this field over the live tracker.

## Where obligations surface

- **`egg-orch consensus status`** — prints a `Pending pre-merge obligations:` subsection for open obligations and a `Resolved within this PR:` subsection (with the satisfying SHA) for any that the reviewer marked resolved. No subsection is printed when its list is empty.
- **`complete_phase` HITL gate** — when any obligations are live, `complete_phase` queues a `choice` HITL decision before allowing the phase to close (see [HITL gate at phase completion](#hitl-gate-at-phase-completion)). Resolution status is preserved through approval so the renderer downstream can demote resolved entries.
- **Auto-created PR body** — a ⚠️-headed `## Pre-merge Obligations` section appears directly after the description, listing only **open** obligations so the merger sees actionable items before scrolling to the diff. Already-resolved obligations render under a ✅-headed `## Resolved within this PR` subsection that does not carry the merge-blocking banner. Both the legacy single-PR path and the slice-DAG **umbrella PR** (terminal slice) render the same section via the shared `orchestrator/pr_obligations.py` module (#2354).
- **`CONSENSUS_ACK_RECEIVED` event** — the condition (and `pre_merge_condition_resolved_in_diff` when set) is included on the event payload and the `handle_ack` return value, so downstream consumers (e.g. HITL gates) can act on it.

## HITL gate at phase completion

When `complete_phase` is called and any reviewer has an active conditional ACK, the orchestrator queues a `choice` HITL decision before allowing the phase to close. The decision lists each obligation and presents three options:

| Option | Effect |
|--------|--------|
| **Approve and accept obligations** | Obligations are written to `contract.pr.deferred_actions` and the phase proceeds. They appear in the auto-created PR body even after the tracker is torn down. |
| **Reject and force NACK** | Each conditioning `(reviewer, producer)` edge is force-NACKed. The producer returns to `WORKING`; a new consensus round is required before the phase can close. |
| **Address in-pipeline (invalidate ACK)** | Each conditioning ACK edge drops back to `PENDING`. The producer must re-propose before the phase can close. |

The gate is skipped when `force=true` is passed to `complete_phase`, so operators can drain stuck pipelines. A second `complete_phase` call while the gate is still pending returns the existing decision id rather than queuing a duplicate.

## Example lifecycle

1. Reviewer runs `consensus ack … --pre-merge-condition "…"`.
2. `egg-orch consensus status` shows the obligation under `Pending pre-merge obligations:`.
3. The producer re-proposes (say, to address another reviewer's NACK). The obligation is no longer scoped to the current version and drops off.
4. The reviewer re-reviews the new version and either re-attaches the condition (conditional ACK again) or ACKs unconditionally.
5. Once consensus is reached, `complete_phase` detects the live condition and queues the 3-way HITL gate.
6. The human selects an option. If they approve, the obligation is persisted to `contract.pr.deferred_actions` and appears in the PR body for the merger.

## In-cycle resolution: satisfying agent path (#2338)

The HITL gate plus PR-body transcription assume the obligation can only be satisfied at merge time. When the conditioning work *can* — and *does* — happen in-cycle, that escalation path becomes busywork. The flow:

1. `reviewer_contract` issues a conditional ACK to the coder: *"Before merging slice-1, the tester must commit mechanical patch-path rewrites in `tests/tools/...`."* The coder role is gateway-blocked from `tests/`.
2. The tester cherry-picks the satisfying commit (`<sha>`) onto the producer branch and pushes.
3. The tester calls:

   ```
   mcp__brc__resolve_obligation \
     reviewer_role="reviewer_contract" \
     producer_role="coder" \
     commit_sha="<sha>" \
     note="Mechanical patch-path rewrites cherry-picked from coder's prior commit"
   ```

4. The matrix marks `obligation_resolved=True` on the `(reviewer_contract, coder)` edge. `get_pre_merge_conditions()` skips it; the HITL gate at `complete_phase` no longer fires for this obligation; the PR body's "Pre-merge Obligations" section omits it.
5. When the coder re-proposes a new version, the matrix's `record_ack` resets the resolved flag — if `reviewer_contract` re-attaches the same obligation, the satisfier must call `resolve_obligation` again. The cleaner path is for `reviewer_contract` to drop the obligation on re-ACK once the satisfying commit is on the branch (see [`code-review-criteria.md`](../../shared/prompts/code-review-criteria.md) §"Drop obligations satisfied in-cycle").

Resolution does not retroactively un-persist a `contract.pr.deferred_actions` entry that the HITL gate already wrote. Resolve before `complete_phase` triggers the gate, or accept that the obligation will appear in the PR body until the next pipeline run.

## Pointers

- Schema: [`orchestrator/attestation_schemas.py`](../../orchestrator/attestation_schemas.py) — `ReviewPayload.pre_merge_condition`, `ReviewPayload.pre_merge_condition_resolved_in_diff`.
- Matrix: [`orchestrator/approval_matrix.py`](../../orchestrator/approval_matrix.py) — `ApprovalEntry.pre_merge_condition`, `ApprovalEntry.pre_merge_condition_resolved_in_diff`, `ApprovalEntry.obligation_resolved`, `get_pre_merge_conditions()`, `mark_obligation_resolved()`.
- Tracker: [`orchestrator/peer_consensus.py`](../../orchestrator/peer_consensus.py) — `handle_ack`, `handle_resolve_obligation`, `get_pre_merge_conditions()`, `evaluate()`.
- HITL gate: [`orchestrator/routes/phases.py`](../../orchestrator/routes/phases.py) — `_ensure_conditional_ack_gate`, `CONDITIONAL_ACK_OPTIONS`.
- Gate dispatch: [`orchestrator/routes/decisions.py`](../../orchestrator/routes/decisions.py) — `_handle_conditional_ack_gate`.
- Resolve signal: [`orchestrator/routes/signals.py`](../../orchestrator/routes/signals.py) — `handle_consensus_resolve_obligation_signal`.
- MCP tool: [`sandbox/egg_agent_tools/tools/brc.py`](../../sandbox/egg_agent_tools/tools/brc.py) — `mcp__brc__resolve_obligation`.
- PR body renderer: [`orchestrator/pr_obligations.py`](../../orchestrator/pr_obligations.py) — `render_obligations_section`, `render_obligations_section_from_normalized`, `normalize_deferred_actions`. Shared by the legacy single-PR path (`_build_pre_merge_obligations_section` in `routes/pipelines.py`) and the slice-DAG umbrella PR path (`GatewayClient.create_slice_pr` in `gateway_client.py`).
- Contract: [`shared/egg_contracts/models.py`](../../shared/egg_contracts/models.py) — `PRMetadata.deferred_actions`, `DeferredAction`.
- CLI: [`sandbox/egg_lib/orch_cli.py`](../../sandbox/egg_lib/orch_cli.py) — `cmd_consensus_ack`, `cmd_consensus_status`.
- Related: [Concurrent Execution: Reviewer verdict variants](../guides/concurrent-execution.md#reviewer-verdict-variants), [HITL Decisions](../hitl-decisions.md), [Orchestrator CLI](orchestrator-cli.md), [Reviewer Sync](../../shared/prompts/REVIEWER-SYNC.md).
