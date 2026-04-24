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

## How obligations are tracked

- **Persistence.** `record_ack` stores the condition on the approval-matrix edge for the current proposal version ([`orchestrator/approval_matrix.py`](../../orchestrator/approval_matrix.py)).
- **Version scoping.** `get_pre_merge_conditions()` returns only conditions attached to each producer's *current* proposal version. If a producer re-proposes, the reviewer's prior conditional ACK is dropped until they re-attach the obligation to the new version.
- **NACK clears it.** A NACK or `invalidate_ack` clears any condition on that edge — there is no such thing as a lingering conditional NACK.

## Where obligations surface

- **`egg-orch consensus status`** — prints a `Pending pre-merge obligations:` subsection listing each `reviewer → producer: condition` line while the pipeline is still live. No subsection is printed when the list is empty.
- **Auto-created PR body** — a ⚠️-headed `## Pre-merge Obligations` section appears directly after the description, listing each active obligation so the merger sees it before scrolling to the diff.
- **`CONSENSUS_ACK_RECEIVED` event** — the condition is included on the event payload and the `handle_ack` return value, so downstream consumers (HITL gates, future automation) can act on it.

## Example lifecycle

1. Reviewer runs `consensus ack … --pre-merge-condition "…"`.
2. `egg-orch consensus status` shows the obligation under `Pending pre-merge obligations:`.
3. The producer re-proposes (say, to address another reviewer's NACK). The obligation is no longer scoped to the current version and drops off.
4. The reviewer re-reviews the new version and either re-attaches the condition (conditional ACK again) or ACKs unconditionally.
5. Once consensus is reached, the PR is opened; any still-active obligations are rendered in the PR body for the merger.

## Pointers

- Schema: [`orchestrator/attestation_schemas.py`](../../orchestrator/attestation_schemas.py) — `ReviewPayload.pre_merge_condition`.
- Matrix: [`orchestrator/approval_matrix.py`](../../orchestrator/approval_matrix.py) — `ApprovalEntry.pre_merge_condition`, `get_pre_merge_conditions()`.
- Tracker: [`orchestrator/peer_consensus.py`](../../orchestrator/peer_consensus.py) — `handle_ack`, `get_pre_merge_conditions()`, `evaluate()`.
- PR body: [`orchestrator/routes/pipelines.py`](../../orchestrator/routes/pipelines.py) — `_build_pre_merge_obligations_section`.
- CLI: [`sandbox/egg_lib/orch_cli.py`](../../sandbox/egg_lib/orch_cli.py) — `cmd_consensus_ack`, `cmd_consensus_status`.
- Related: [Concurrent Execution: Reviewer verdict variants](../guides/concurrent-execution.md#reviewer-verdict-variants), [Orchestrator CLI](orchestrator-cli.md), [Reviewer Sync](../../shared/prompts/REVIEWER-SYNC.md).
