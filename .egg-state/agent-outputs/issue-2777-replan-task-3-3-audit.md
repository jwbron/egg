# task-3-3 (#2570) silent-rebase audit

> Pipeline: `issue-2777-replan` | Phase: implement | Slice: 3 (1c) | Role: coder
> HEAD at audit time: see `git rev-parse HEAD` in the commit linking this artifact.

## Scope

Per slice-3 plan task-3-3, audit the orchestrator's silent-rebase of
`egg/<id>/work` onto `main` (#2570) and identify the precise vector,
then either (a) fix it in-scope or (b) trigger AC-9a's HITL escalation
because the vector lives inside an OOS primitive.

## Audit sites visited

All citations re-anchored against the slice-3 HEAD (commit
`a654ab2ee` after task-3-1 + task-3-2; the post-edit line numbers
quoted here reflect the post-edit state — the absolute line numbers
shift again on subsequent commits in this slice; symbol names +
adjacent strings are the stable anchors).

| Symbol | file:line | OOS? | Notes |
| --- | --- | --- | --- |
| `_sync_worktree_with_remote` | `orchestrator/routes/pipelines.py:6872` | **YES** (decision-11 / cq-7) | ~538 lines. Called from `_run_pipeline` at the `phase_start_sync_outcome` and `post_phase_sync_outcome` sites. |
| `_rebase_pipeline_branch_onto_base` | `orchestrator/routes/pipelines.py:7411` | **partial** — wrapper layer; *callees* it touches are inside `_sync_worktree_with_remote`'s sibling helpers | ~313 lines. Sole orchestrator caller is `_run_pipeline` at the resume-time rebase site. Public test surface is `orchestrator/tests/test_rebase_pipeline_branch.py`. |
| `_build_rebase_cmd` (helper) | bare-rebase fallback formed inside `_sync_worktree_with_remote`'s `local_ahead > 0 and remote_ahead > 0` branch | **YES** (within the OOS function body) | The fallback is the actual contamination vector: when `base_branch_for_reconcile is None`, the helper emits `git rebase origin/<branch>` (the bare form) instead of `git rebase --onto origin/<branch> origin/<base_branch>` (the safer onto-form). |

## Diagnosis

The #2570 silent rebase / divergence-rebase shape resolves to the
**`local_ahead > 0 and remote_ahead > 0` branch inside
`_sync_worktree_with_remote`**, specifically the *bare-rebase fallback*
the code itself documents (line range corresponds to the comment block
that begins "⚠️ When ``base_branch_for_reconcile`` is None,
``_build_rebase_cmd`` falls back to the plain ``git rebase
origin/{branch}`` form — the same form that triggered #2222
main-contamination on the gateway-side push-reject path.").

The vector is:

1. `_run_pipeline` invokes `_sync_worktree_with_remote(..., base_branch_for_reconcile=...)` at the phase-start and post-phase boundaries.
2. When the call site supplies `base_branch_for_reconcile=None` (and several do, historically), the function reaches the `local_ahead > 0 and remote_ahead > 0` branch and dispatches to `_rebase_with_agent_output_autoresolve` with `base_branch=None`.
3. `_build_rebase_cmd` returns the bare `git rebase origin/<branch>` form. With HEAD at the post-rebase main tip and `origin/<branch>` on the stale pipeline snapshot, the rebase replays merge-base..HEAD on the stale tip — producing duplicate-by-content commits that look like a silent rebase of `egg/<id>/work` onto `main`.
4. The post-rebase `egg/<id>/work` then merges via `git merge-base origin/main origin/egg/<id>/work` to a different (newer) SHA than the pipeline-creation SHA — the exact symptom #2570 reports.

The downstream `#2792` auto-recovery (hard-reset back to
`origin/<base>`) is the recovery layer wired below this branch and
does NOT prevent the bare-rebase from running first.

The cited evidence at HEAD: the `_sync_worktree_with_remote` body
already names the vector with the comment "That fallback is the
contamination vector" + `(#2222 contamination risk)` WARN log
emitted at the call site (see surrounding code for the literal
strings).

## OOS scope check (AC-9a hard requirement)

`_sync_worktree_with_remote` is in the
`explicitly_out_of_scope.files_or_symbols` list per cq-7 /
decision-11 (the refine analysis documents the operator's directive
that the divergence root-cause work is reserved for #2792 / the
plan_draft_missing_on_local recovery work).

`_rebase_pipeline_branch_onto_base` is technically not in the OOS
list as a top-level symbol, BUT modifying it without modifying the
sibling fallback inside `_sync_worktree_with_remote` would not fix
the vector — the fallback IS the vector, and it lives inside the
OOS function body.

**AC-9a fires.** Per the plan's iteration-0 EXPECTATION note:

> The AC-9a gate below WILL fire by construction on the first audit
> pass. Do not treat this as a surprise discovery — the expected
> resolution path is AC-9a option 3 ("Mark #2570 as xfail in slice-3
> and open a follow-up issue co-scheduled with the #2792 work").

The implementer-phase coder MUST register the HITL via
`mcp__sdlc__register_open_question` with three options before any
code change:

1. Extend scope to include the OOS primitive in slice-3 — operator overrides decision-11.
2. Defer slice-3 until the #2792 work lands — wait for the OOS-coupled work.
3. Mark #2570 as xfail in slice-3 and open a follow-up issue for the OOS-coupled fix — ship slice-3 without the #2570 fix.

Default recommendation: **option 3** (per plan's R1).

## Chosen path

Registering the HITL with the three options. No code change to
`_sync_worktree_with_remote` or `_rebase_pipeline_branch_onto_base`
or `_build_rebase_cmd` from this task. The #2570 regression test in
TASK-3-11 should either xfail (option 3) or be deferred (option 2)
per the operator's resolution. If option 1 is chosen, this task
re-opens to apply the in-scope fix.
