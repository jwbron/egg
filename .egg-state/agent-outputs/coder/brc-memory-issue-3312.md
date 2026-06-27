# coder BRC memory — issue #3312, slice-13

## Verdict: PROPOSED (orchestrator/mcp_tools.py decomposition)
- Slice-13 target: orchestrator/mcp_tools.py (2,948 lines / 130,445 bytes — OVER 100KB byte cap)
  -> sub-package orchestrator/mcp_tools/ (method-modules-on-class, §c; same shape as
  slice-8 overseer/monitor/ + slice-10 peer_consensus/).
- Branch: egg/issue-3312-slice-13-coder/work (base = slice-12 landed @ 067f8f250).

## What landed (coder-owned commits, in order)
1. 5df8225fb step-0 baseline: pure git mv mcp_tools.py -> mcp_tools/__init__.py (byte-identical).
2. 200467dbf decompose: 11 submodules + barrel (PIPELINE_TOOLS -> _tool_defs.py).
3. 7ddc1b5ea drop allowlist entry (scripts/file-size-allowlist.yaml line 29).
4. 29749cdcb orchestrator/Dockerfile: explicit `COPY orchestrator/mcp_tools/ ./mcp_tools/`
   (after peer_consensus/ line). Non-recursive `orchestrator/*.py` glob misses the new dir.
- orchestrator/CLAUDE.md mcp_tools/ seam-table subsection: DOCUMENTER-OWNED (coder role-blocked
  by shared/egg_restrictions/patterns.py; alternative_role=documenter). Drafted content handed off
  in .egg-state/agent-outputs/coder/slice-13-claude-md-seam-row.md; documenter appends it.

## Cluster layout (class-dominated; method-modules-on-class)
Largest submodule _tool_defs.py = 1,069 lines / 49KB — all under 1,500-line / 100KB hard cap.
(_tool_defs.py trips only the 800-line SOFT-cap warning; check-file-sizes.py exits 0.)
- __init__ (196)  PipelineToolHandler class def + __init__ + 40 method bindings; preamble keeps
  module globals logger/cap_result_dict/_TOOL_NARROW_HINTS/_is_timeout_error/GATEWAY_PORT/
  _SLICE_ID_PATTERN; re-exports PIPELINE_TOOLS; __all__ = [PIPELINE_TOOLS, PipelineToolHandler].
- _tool_defs (1069)  PIPELINE_TOOLS schema list (pure data). 31 tools.
- _dispatch (86)   handle_tool_call (dispatch dict + cap_result_dict).
- _request (134)   _make_request, _get_gateway_client, _ensure_gateway_session, _make_gateway_request.
- _submit (266)    _handle_submit_task, _handle_validate_config, _handle_update_pipeline_config, _handle_populate_contract.
- _status (397)    _handle_get_status, _live_running_agents_fallback, _build_status_snapshot, _enrich_pending_decisions, _read_reviewer_feedback, _handle_get_phase.
- _tasks (143)     _handle_provide_input, _handle_answer_feedback, _handle_list_tasks, _handle_cancel_task.
- _health (153)    _handle_check_health, _handle_list_containers, _handle_get_container_logs, _handle_send_message.
- _consensus (142) _handle_get_consensus_status, _infer_consensus_from_messages.
- _snapshot (115)  _handle_get_pipeline_snapshot, _handle_get_contract.
- _lifecycle (286) _handle_restart_agent, _handle_restart_phase, _handle_list_agent_local_commits, _handle_salvage_agent_commits, _handle_advance_phase, _handle_start_pipeline, _handle_start_phase, _handle_complete_phase.
- _deployment (223) _handle_get_deployment_context, _handle_validate_deployment_manifests, _handle_prune_stale_worktrees, _handle_validate_network_isolation, _handle_get_service_logs, _handle_rebuild_and_rollout.

## Correctness posture (pure refactor proof)
- AST-equivalence: all 42 PipelineToolHandler methods (40 _handle_* + __init__ + handle_tool_call etc.)
  are CODE-AST-identical (docstring-stripped) to the pre-split file. The ONLY delta is docstring
  re-indentation (method dedent-by-4 shifts triple-quoted docstring interior lines). Proven by AST
  diff: non-docstring multi-line string constants are byte-identical (zero behavior change).
- PIPELINE_TOOLS ast.dump identical to pre-split.
- Patch seams: suite uses patch.object(handler, "_make_request") (instance-level — resolves via class
  binding after the split) and patch("urllib.request.build_opener") (external). There are NO
  patch("mcp_tools.X") module-global string seams (grep-verified). So submodules import barrel globals
  (logger, cap_result_dict, _TOOL_NARROW_HINTS, _is_timeout_error, GATEWAY_PORT, _SLICE_ID_PATTERN)
  via `from mcp_tools import ...` and keep a single binding — mirrors peer_consensus slice-10 exactly.
- External importers (audit): ONLY `from mcp_tools import PIPELINE_TOOLS, PipelineToolHandler`
  (orchestrator/mcp_server.py + the test suite). Both re-export through the barrel. shared/egg_tool_output.py
  reference is a comment only. => zero test-file edits needed (task-13-6 patch-rewrites = no-op for this slice).
- Method-local imports (json, HTTPError, Request/build_opener, PipelineConfig, ValidationError, urlparse,
  time, urllib.parse.urlparse) stay inside the bodies verbatim — not hoisted.

## Tests / lint
- ruff check + ruff format --check: All checks passed (12 files). check-file-sizes.py exit 0
  (no mcp_tools warning beyond _tool_defs soft cap; no stale-allowlist error).
- pytest (system 3.14 / pytest 9.1.1, NO venv): tests/test_mcp_tools.py + _enrichment + _salvage +
  test_restart_mcp_tools.py = 219 passed. Cross-tree importers: test_mcp_server + test_source_branch +
  test_short_flow_contract_population + test_lifecycle_empty_body = 126 passed.
- Dockerfile: docker unavailable -> container-layout import smoke reproduced the COPY graph:
  (A) non-recursive glob alone -> ModuleNotFoundError: No module named 'mcp_tools';
  (B) with explicit `COPY orchestrator/mcp_tools/ ./mcp_tools/` -> import OK (31 tools). Necessary+sufficient.
- No .venv locally (uv sync cert error) -> make test-all not run locally; CI's pinned venv runs it green.

## Anticipated reviewer questions
- "Why import barrel globals into submodules instead of _pkg.-prefixing?" No patch("mcp_tools.X") seams
  exist (none of logger/cap_result_dict/etc are test-patched), so a value import is faithful and keeps a
  single binding — same call peer_consensus slice-10 made.
- "_tool_defs.py 1069 lines" — under the 1,500 hard cap; only the 800 soft-cap advisory warning, which
  many allowlist-free modules trip (select_tests/_graph.py, models.py). Pure schema data; further split
  adds no value.
- "Docstring re-indentation" — cosmetic dedent artifact, identical to slice-8/slice-10; __doc__ only,
  no runtime effect; non-docstring strings byte-identical.
