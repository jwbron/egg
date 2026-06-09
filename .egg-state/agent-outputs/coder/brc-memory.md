# coder BRC memory — pipeline issue-3023 slice-3

## Last reviewed commit SHA

a056e50dbc15fee1e5d9103d038d2e87db1051fa (v2 re-propose)

## Decision log

### 2026-06-09 — v2 re-propose addressing reviewer_code_holistic v1 NACK

Context: v1 of the coder proposal for #3023 slice-3 (TASK-3-1 / 3-2 / 3-3
/ 3-5) was holistically NACKed by reviewer_code_holistic with three
specific items, all low-effort and all within coder-writable scope plus
one documenter-owned cross-cutting find:

1. `integration_tests/regression/test_brc_concurrency.py:53` — the
   module docstring referenced `orchestrator/tests/test_consensus_wrapper.py`
   as the surviving event-pump coverage. That file was deleted in
   TASK-3-1. Rewrote the docstring to point at:
   - `test_concurrent_integration.py::test_orchestrator_drives_event_pump_loop`
     (TASK-3-1 import-level guard),
   - `shared/tests/test_egg_agent_signal.py` (TASK-3-3 SIGTERM trap),
   - `test_concurrent_executor.py` + the slice-3 tester TDD scaffolds
     (commits 42ea6ea, 6034c62, 8f27291, 47b2ce2).
2. `tests/shared/egg_agent/test_client.py:1180` — the inline comment
   appealed to the wrapper's `is_buffer_overflow` grep for the assertion's
   rationale. That helper was deleted in TASK-3-1. Rewrote both the
   class docstring and the inline comment to anchor the rationale in
   the surviving surface contract (the marker is the agreed-upon
   error-string token between `run_agent`'s CLI-JSON-decode path and
   any downstream classifier — wrapper-side grep is gone, but the
   surface contract on `result.error` survives).
3. **Pre-merge condition scope.** Reviewer found `docs/guides/per-agent-
   models.md:141` (a documenter-owned table cell) still claims
   `_spawn_agent` calls `build_consensus_wrapped_command(...)`, which is
   factually wrong post-slice-3 (`_spawn_agent` now passes
   `command=None`). Coder cannot write to `docs/guides/per-agent-models.md`
   (`check_file_restriction` confirms `role 'coder' is blocked from
   'docs/guides/per-agent-models.md' by patterns.py`; alternative_role
   = documenter). Extended the pre-merge condition on the v2 re-propose
   to name `docs/guides/per-agent-models.md` alongside
   `orchestrator/README.md` so the documenter follow-up isn't lost.

Cross-cutting concerns the reviewer flagged as "no blockers, FYI":

- (a) `spawn_all` docstring naming itself a misnomer pending rename:
  intentional and already candid in the v1 docstring; no change.
- (b) `_spawn_roles` still calls `self._spawn_agent(role, prompt_text)`
  with `del prompt_text`: cq-4 follow-up will retire `_spawn_roles`
  outright; deferred.
- (c) `_is_on_demand_in_flight` permissively flags ANY non-WORKING /
  non-REVIEWING tracker phase as in-flight: added a one-line
  clarification to the docstring explaining the CONFIRMED-but-not-
  cleaned-up case is intentional (running orchestrator's tick
  re-derives next-action and either advances or surfaces a
  stuck-phase-transition alert).

### Architectural invariants preserved across v1→v2

- TASK-3-1 module deletion remains clean: `orchestrator/consensus_wrapper.py`
  and `orchestrator/tests/test_consensus_wrapper.py` are gone; no
  production matches for `consensus_wrapper|build_consensus_wrapped_command|
  EVENT_PUMP_|is_buffer_overflow|is_transient_crash|is_startup_failure`
  in `orchestrator/ sandbox/ shared/`.
- TASK-3-2 `spawn_all` → tracker-registration-only collapse, with
  explicit cq-4 deferral comments naming the rename follow-up.
- TASK-3-3 SIGTERM trap in `shared/egg_agent/__main__.py`: lock-guarded
  `_shutdown_logged`, main-thread check raising `RuntimeError`,
  `exit(0)` per kubelet contract, best-effort install with WARN
  fallback in `main()`, audit line on stderr to survive stdout
  buffering.
- TASK-3-5 `_is_on_demand_in_flight` heuristic: lazy imports, try/except
  wrappers, fall-through to the strictly-safer mark-FAILED path on
  undecidable cross-version state.

## NACK history

### v1 — reviewer_code_holistic NACK (2026-06-09)

Three blocking items, all addressed in v2 (see decision log above).
Other reviewer_code_holistic findings (architecture, TASK-3-1/2/3/5
correctness, test rewires) were named clean and not re-reviewed in v2.

## Style / convention notes

- Cite issue numbers as `#NNNN` after first mention; first mention can
  use a markdown link to the GitHub issue URL.
- For test-file docstrings, prefer ASCII pointer lists over prose for
  multi-target coverage references — easier to grep, easier to update
  when a target moves.
