# Documenter handoff — sandbox/CLAUDE.md seam subsection for `egg_lib/orch_cli/` (slice-17, #3312)

`sandbox/CLAUDE.md` is **documenter-owned** (coder is role-blocked by
`shared/egg_restrictions/patterns.py`; `check_file_restriction` →
`alternative_role=documenter`). This is the ready-to-paste subsection to add
under the existing **"## Decomposition seams"** section, appended after the
last landed subsection (currently `entrypoint/` — slice 9).

---

## Paste this subsection:

### `egg_lib/orch_cli/` — `egg-orch` CLI ([#3312](https://github.com/jwbron/egg/issues/3312), slice 17)

`orch_cli.py` (5,012 lines / 190,656 bytes — **over BOTH the line and byte cap**, the largest of the simpler-than-`pipelines.py`/`gateway.py` targets) → `egg_lib/orch_cli/` (largest submodule `_parser.py`, 1,355 lines — under the 1,500-line cap; the soft-cap warning is non-fatal and precedented by slice-15/16). Argparse command surface over the orchestrator + gateway APIs, grouped by **subcommand family**. `main()` lives in the barrel; the barrel does explicit per-symbol re-exports and declares `__all__`, preserving the full public API.

**Entry point (path-style, NOT a Dockerfile change):** `bin/egg-orch` was a symlink to the single-file `orch_cli.py`. Because the module became a package, direct execution of `__init__.py` would break relative imports, so the slice adds `orch_cli/__main__.py` (a thin path-fixup shim that prepends the sandbox root to `sys.path` and resolves `main` through the barrel, mirroring `scripts/select_tests/__main__.py`) and repoints the `bin/egg-orch` symlink at `../egg_lib/orch_cli/__main__.py`. **Container packaging is neutral** — unlike the `orchestrator/`/`gateway/` slices (non-recursive `COPY *.py` globs) and unlike `entrypoint/` (single-file `COPY` + `ENTRYPOINT`), the sandbox image ships `egg_lib/` via the **recursive** `COPY . /opt/egg-runtime/` (Dockerfile:361) with `PYTHONPATH=/opt/egg-runtime/sandbox`, so `egg_lib/orch_cli/` is auto-included; `chmod +x …/bin/*` (Dockerfile:362) keeps the repointed symlink executable. No Dockerfile edit required.

| Submodule | Responsibility | Key symbols |
|-----------|----------------|-------------|
| `__init__.py` (barrel, 324 lines) | Stable public API: eager submodule imports + explicit per-symbol re-exports + `main()` + `__all__`. Re-exports the two live patch seams `orch_request` / `get_agent_role_from_env` so `patch("egg_lib.orch_cli.<seam>")` resolves | `main`, `create_parser`, `orch_request`, `get_agent_role_from_env`, + the full `cmd_*` surface |
| `__main__.py` (33 lines) | Path-style entry shim for the `egg-orch` symlink; `sys.path` fixup → `from egg_lib.orch_cli import main` | `main` (re-resolved) |
| `_http.py` (325 lines) | HTTP/transport infra: API request helpers, URL/env/token resolution, ID validation, JSON output, the `_opener` + `_SAFE_ID_PATTERN`/`_SLICE_ID_PATTERN` globals. **Defines** both patch seams | `ApiError`, `api_request`, `api_request_or_exit`, `orch_request`, `gateway_request`, `get_agent_role_from_env`, `get_session_token`, `validate_id`, `require_pipeline_id`, `print_json`, `resolve_slice_id` |
| `_common.py` (≈230 lines) | Shared CLI helpers: role resolution + handler-error rendering + prose-arg plumbing (stdin/file channels) | `_require_role`, `_render_handler_error`, `_ProseArgError`, `_emit_argv_prose_deprecation`, `_resolve_prose_arg`, `_resolve_files_reviewed_arg` |
| `_health.py` (≈245 lines) | Health + gateway-introspection subcommands | `cmd_health`, `cmd_gateway_health`, `cmd_gateway_phase`, `cmd_gateway_permissions`, `cmd_health_alerts`, `cmd_health_resolve` |
| `_pipeline.py` (≈319 lines) | Pipeline-lifecycle subcommands + wait-status terminal-state constants | `cmd_pipeline_list/get/create/status/delete/wait_status`, `_WAIT_STATUS_TERMINAL_STATUSES`, `_WAIT_STATUS_TO_EVENT_TYPE` |
| `_signal.py` (≈187 lines) | Signal subcommands | `cmd_signal_complete/progress/error/heartbeat/readiness` |
| `_phase.py` (≈154 lines) | Phase subcommands | `cmd_phase_get/advance/start/complete/get_context` |
| `_decision.py` (≈151 lines) | Decision-queue subcommands | `cmd_decision_list/create/resolve/status` |
| `_container.py` (≈160 lines) | Container subcommands | `cmd_container_list/spawn/get/stop/logs` |
| `_message.py` (684 lines) | Inter-agent message subcommands + wait-cursor file helpers | `cmd_message_send/poll/wait/wait_loop/heartbeat/status`, `_wait_cursor_path`, `_read_cursor_file`, `_write_cursor_file`, `_delete_cursor_file`, `_classify_gateway_error_rc`, `_resolve_from_producer_arg` |
| `_overseer.py` (538 lines) | Overseer subcommands + label/limit constants | `cmd_overseer_alert/file_issue/consult_advisor`, `_OVERSEER_TITLE_MAX_CHARS`, `_OVERSEER_BODY_MAX_BYTES`, `_OVERSEER_VALID_LABEL_PRIORITIES` |
| `_consensus.py` (541 lines) | BRC consensus subcommands | `cmd_consensus_propose/ack/nack/withdraw/confirmed/status`, `_consensus_push`, `_render_stale_version_rejection` |
| `_brc.py` (≈276 lines) | BRC inspection subcommands + valid-phase constant | `cmd_brc_next_action/get_state/list_blocking/resolve_obligation/read_peer_artifact`, `_VALID_BRC_HISTORY_PHASES` |
| `_progress.py` (≈154 lines) | Progress/env subcommands | `cmd_env`, `cmd_progress_emit`, `cmd_progress_query` |
| `_parser.py` (largest, 1,355 lines) | argparse wiring: `create_parser()` builds the full subparser tree. Dispatch targets resolve through the barrel (`func=_pkg.cmd_*`) | `create_parser`, `_add_json_flag`, `_non_negative_int` |

**Pure refactor, no behaviour change.** Proven by reconstruction: all 4,315 non-blank code lines of the pre-split body reappear verbatim across the submodules once the `_pkg.` seam indirection is reversed (0 lines missing). **Patch seams preserved:** the two seams patched in the test suite — `orch_request` (`@patch("egg_lib.orch_cli.orch_request")` in `test_phase_cli` / `test_brc_cli_args`, and `monkeypatch.setattr(orch_cli, "orch_request", …)` in `test_cli_session_state`) and `get_agent_role_from_env` (`patch("egg_lib.orch_cli.get_agent_role_from_env")` in `test_message_wait_cli`) — are **defined in `_http.py`, re-exported on the barrel, and reached from every command submodule via `import egg_lib.orch_cli as _pkg` (live barrel-attribute lookup)**, so the barrel-level patches keep intercepting. Every externally-imported symbol (`cmd_consensus_*`, `cmd_phase_*`, `cmd_overseer_*`, `cmd_message_poll`, `create_parser`, `_render_stale_version_rejection`, `_wait_cursor_path`, the `_OVERSEER_*` constants, …) is re-exported through the barrel, so the importers — `cli_session_state.py`, `session_state_sync.py`, the `egg_agent_tools.handlers` / `push` modules, and the test suite — need **zero edits**. `ruff check` + `ruff format` clean (the lone `_parser.py` 1,355-line soft-cap warning is non-fatal). The orch_cli test suite shows the identical **203 passed / 10 failed** result as the step-0 baseline — the 10 failures are pre-existing `test_message_wait_cli` cursor-file env failures present on `HEAD` under the same interpreter, **not split-induced**.

---

## v2 UPDATE (after reviewer_code / reviewer_code_holistic NACK on v1) — DOCUMENTER ACTION

The slice-17 subsection already in `sandbox/CLAUDE.md` says the suite patches **"the two seams"**
(`orch_request`, `get_agent_role_from_env`). That is now INACCURATE — please correct it to the
full seam surface below. The v1 seam audit only covered `sandbox/tests/` and missed the top-level
`tests/sandbox/` tree.

**Full barrel patch-seam surface (six functions + one object):**
- Functions routed via `import egg_lib.orch_cli as _pkg` → `_pkg.<sym>` at EVERY call site
  (incl. `_http`'s own `api_request` calls): `orch_request`, `get_agent_role_from_env`,
  `api_request`, `_consensus_push`, `require_pipeline_id`, `_require_role`.
- Object re-exported on the barrel (no `_pkg.` — shared object, mutated in place):
  `_opener` (urllib `build_opener` instance) → `patch.object(orch_cli._opener, "open", …)`.

**Corrected verification line:** full `tests/sandbox` orch_cli set 317 passed / 0 failed;
`handlers_brc` + `mcp_cli_drift` + `sdlc_hitl` 338 passed; `sandbox/tests` orch_cli suite
203 passed / 10 pre-existing `test_message_wait_cli` cursor-file env failures (unchanged from
baseline, not split-induced).
