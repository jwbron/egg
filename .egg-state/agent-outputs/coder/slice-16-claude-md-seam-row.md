# Documenter handoff — orchestrator/CLAUDE.md seam row for `gateway_client/` (slice-16, #3312)

`orchestrator/CLAUDE.md` is **documenter-owned** (coder is role-blocked by
`shared/egg_restrictions/patterns.py`; `check_file_restriction` →
`alternative_role=documenter`). This is the ready-to-paste seam subsection the
documenter should add under the "Decomposition seams" section, immediately
before the closing "landed `orchestrator/` decompositions" paragraph — and that
closing paragraph must be updated to append `gateway_client/` to the list.

Two small edits elsewhere in the same file (optional but accurate):
- Module-layout table, "External clients" row: change `gateway_client.py` →
  `gateway_client/`.

---

## Paste this subsection (after the `routes/signals/` subsection):

### `gateway_client/` — orchestrator↔gateway HTTP client ([#3312](https://github.com/jwbron/egg/issues/3312), slice 16)

`gateway_client.py` (4,326 lines / 183,370 bytes — **over BOTH the line and byte cap**) → `gateway_client/` (largest submodule `_push.py`, 807 lines). Class-dominated module: follows the **method-modules-on-class** shape ([pattern](../docs/guides/decomposition-pattern.md) §c) rather than the Flask-blueprint shape. The `GatewayClient` class definition + `__init__` + the `base_url` / `self_ip` properties + `_resolve_self_ip` + the `GatewayError` / `GatewayConnectionError` exceptions + the `get_gateway_client` singleton factory + `validate_security_boundary` stay in the barrel and keep the class identity on the `gateway_client` module path; each of its other ~35 method bodies moves to a responsibility-grouped private submodule as a module-level function taking `self` explicitly, bound back onto the class in the barrel. The 16 top-level helpers / dataclasses (the `SessionInfo` / `WorktreeResult` / `GatewayHealth` / `PushResult` dataclasses, the PR-body formatters, the rebase-mechanic helpers, `_classify_push_stderr`) move alongside and the externally-referenced ones are re-exported. **Dockerfile (binding, NOT packaging-neutral):** `gateway_client.py` was a top-level `orchestrator/` module shipped by the non-recursive `COPY orchestrator/*.py ./` glob (Dockerfile:44); once it becomes a directory the glob stops matching it, so this slice adds an explicit `COPY orchestrator/gateway_client/ ./gateway_client/` (Dockerfile) to keep `import gateway_client` resolving in the image — same as slice-3 `state_store/`, slice-10 `peer_consensus/`, slice-13 `mcp_tools/`, slice-14 `kubernetes_spawner/`.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel, 297 lines) | Stable public API: the `GatewayClient` class definition + `__init__` + `base_url`/`self_ip`/`_resolve_self_ip` + `GatewayError`/`GatewayConnectionError` + `get_gateway_client` + `validate_security_boundary` + every extracted-method binding; keeps the patched module globals (`logger`, `urlopen`, `subprocess`) imported so their patch seams resolve; re-exports the dataclasses + `_truncate_title` / `_classify_push_stderr` / `_rebase_with_agent_output_autoresolve`; `__all__` lists the re-export surface | `GatewayClient`, `GatewayError`, `GatewayConnectionError`, `GatewayHealth`, `PushResult`, `SessionInfo`, `WorktreeResult`, `get_gateway_client`, `validate_security_boundary` |
| `_models.py` (70 lines) | Gateway client dataclasses | `SessionInfo`, `WorktreeResult`, `GatewayHealth`, `PushResult` |
| `_request.py` (262 lines) | HTTP request plumbing + health checks | `_make_request`, `_retry_transient`, `check_health`, `wait_for_healthy` |
| `_session.py` (341 lines) | Session register / validate / delete / heartbeat | `register_session`, `validate_session`, `delete_session`, `update_session`, `delete_session_by_container`, `heartbeat_session_by_container` |
| `_worktree.py` (140 lines) | Gateway worktree create / delete | `create_worktrees`, `delete_worktrees` |
| `_push.py` (largest, 807 lines) | Branch push + push-reconciliation rebase mechanics | `push_worktree_branch`, `_do_push`, `_reconcile_and_retry_push`, `delete_remote_branch`, `_classify_push_stderr`, `_rebase_with_agent_output_autoresolve`, `_autostash_pop_conflict_result`, `_list_unmerged_paths`, `_build_rebase_cmd`, `_abort_rebase_best_effort` |
| `_pr_format.py` (264 lines) | PR-body formatting helpers (titles, sections, slugs) | `_truncate_title`, `_derive_program_slug`, `_format_position_marker`, `_format_slice_title`, `_first_sentence`, `_append_task_bullets`, `_append_this_slice_section`, `_append_diff_summary_section`, `_format_stack_block` |
| `_pr.py` (615 lines) | PR create / update / list / lookup + repo visibility | `create_pr`, `create_slice_pr`, `update_pr_body`, `list_open_prs`, `lookup_open_pr`, `get_repo_visibility` |
| `_rebase.py` (280 lines) | `rebase_onto` + canonical `rebase --onto` argv builder | `rebase_onto`, `_build_rebase_onto_args` |
| `_merge.py` (472 lines) | merge-base / ancestry / slice-merge + evidence-reachability checks | `merge_base`, `_sha_is_ancestor`, `is_slice_branch_merged_into_parent`, `find_unreachable_evidence_commits` |
| `_integration.py` (471 lines) | `create_slice_integration_branch` — slice integration-branch lifecycle | `create_slice_integration_branch` |
| `_branches.py` (508 lines) | Remote-branch list / fetch / ls-remote / sha lookups | `list_remote_branches`, `list_remote_branches_with_shas`, `fetch_worktree_branch`, `fetch_branch`, `_ls_remote_branch_impl`, `ls_remote_branch`, `ls_remote_branch_strict`, `get_remote_branch_sha` |

Pure refactor, no behaviour change: all 64 symbols are **AST-identical** to the pre-split file after unwrapping the `_pkg.`-prefixing and docstring re-indentation (proven by an AST diff over every moved function/class). Patch seams preserved: the `GatewayClient` class + its method bindings resolve on the barrel, so `patch("gateway_client.GatewayClient")` / `patch.object(GatewayClient, …)` and instance-method calls keep working; the private submodules reach the patched module globals via `import gateway_client as _pkg` (so `patch("gateway_client.urlopen")` and `patch("gateway_client.logger")` keep intercepting) and call `subprocess.run` through the shared `subprocess` module object (so `patch("gateway_client.subprocess.run")` keeps intercepting); `get_gateway_client` stays in the barrel so its 23 patch sites resolve unchanged. The barrel re-exports every externally-imported symbol (the four dataclasses, both exceptions, `_truncate_title`, `_classify_push_stderr`, `_rebase_with_agent_output_autoresolve`), so external importers — `container_spawner` shim, `redaction.py`, `kubernetes_spawner/`, `mcp_tools/`, `routes/pipelines.py`, `cli.py`, `agent_salvage.py`, the test suite — need no edits. `make lint` clean (the `_push.py` 807-line soft-cap warning is non-fatal, precedented by slice-15); gateway_client + cross-importer tests show the identical 128-passed result as the step-0 baseline (the 7 failed / 37 errored are pre-existing sandbox port-bind / gateway-unreachable env failures, not split-induced). The lone non-mechanical touch is a `# noqa: UP047` on `_retry_transient` — the method body is moved verbatim; UP047 fires only because the method is now a module-level generic function (the rule is exempt for methods pre-split), so the suppression keeps the move byte-for-byte rather than introducing a PEP 695 rewrite.

## Update the closing paragraph to:

`routes/decisions/`, `state_store/`, `routes/phases/`, `routes/deployment/`, `routes/event_prompt/`, `overseer/monitor/`, `peer_consensus/`, `mcp_tools/`, `kubernetes_spawner/`, `routes/signals/`, and `gateway_client/` are the landed `orchestrator/` decompositions; later orchestrator slices append their own subsections here.
