# coder BRC memory — pipeline issue-3023 slice-3

## Last reviewed commit SHA

HEAD on origin/egg/issue-3023/slice-3 (v3 re-propose)

## Decision log

### 2026-06-09 — v3 re-propose addressing reviewer_code_holistic v2 NACK

Context: reviewer_code_holistic NACKed v2 claiming the v1→v2 delta was
empty (their `git log 5496e7c182..HEAD --not origin/main -p` returned
empty), and re-asserted the same three v1 blockers. Direct verification
at HEAD on origin/egg/issue-3023/slice-3 contradicts the empty-delta
premise: items 1 and 2 were materially addressed in commit `a056e50db`
(`docs(#3023 slice-3): refresh post-wrapper-retirement test/code
comments (coder v2)`), which is on origin and visible in `git log
origin/main..HEAD` between the feat commit `5496e7c18` and the v2
brc-memory chore `1422be5b5`. Re-reading the two files at HEAD confirms:

- `integration_tests/regression/test_brc_concurrency.py:56-71` —
  docstring already points at `test_concurrent_integration.py::
  test_orchestrator_drives_event_pump_loop`, `shared/tests/
  test_egg_agent_signal.py`, `test_concurrent_executor.py`, and the
  slice-3 TDD scaffold commits (42ea6ea, 6034c62, 8f27291, 47b2ce2).
  Item 1 verifiably addressed at HEAD.
- `tests/shared/egg_agent/test_client.py:1160-1196` — class docstring
  and the inline comment at L1189-1195 already anchor the marker
  rationale in the surviving surface contract (the `result.error`
  token between `run_agent`'s CLI-JSON-decode path and any downstream
  classifier; the wrapper-side `is_buffer_overflow` grep was deleted
  in TASK-3-1). Item 2 verifiably addressed at HEAD.

Concluding the reviewer's `5496e7c182..HEAD` invocation ran against a
stale local fetch (the a056e50db doc-refresh commit was pushed before
the v2 propose). The v3 re-propose explicitly points commit_sha at the
current HEAD on origin/egg/issue-3023/slice-3 so the re-review walks
v1→v3 inclusive of a056e50db and the v3 brc-memory chore.

Item 3 (`docs/guides/per-agent-models.md:141`): coder is structurally
blocked from writing the file (`check_file_restriction` returns
`role 'coder' is blocked … alternative_role=documenter`). v3 takes the
reviewer's recommended option A: extend the pre_merge_condition list
on the propose AND name the extended scope in the v3 commit body so
the documenter follow-up isn't lost. The documenter has already
CONFIRMED in slice-3 BRC (state matrix shows `documenter: confirmed:
true, producer_phase: CONFIRMED`); the pre-merge condition lands as a
pre-merge obligation on the auto-created PR (#1998), not a re-open of
documenter's BRC cycle. Specifically the obligation extends from:

  orchestrator/README.md:278 (AC-R10 doc-side update, named on v1/v2)

…to also cover:

  docs/guides/per-agent-models.md:141 (table cell still claims
  `_spawn_agent` calls `build_consensus_wrapped_command(model=
  decision.claude_code_alias, …)`, which is factually wrong
  post-#3023 slice-3 — `_spawn_agent` now passes `command=None`
  unconditionally per TASK-3-2).

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

### v2 — reviewer_code_holistic NACK (2026-06-09)

Stale-view re-NACK premised on `git log 5496e7c182..HEAD --not
origin/main -p` returning empty. Direct file read at HEAD on
origin/egg/issue-3023/slice-3 contradicts the premise: a056e50db
(`docs(#3023 slice-3): refresh post-wrapper-retirement test/code
comments (coder v2)`) is on origin between 5496e7c18 and the v2
brc-memory chore 1422be5b5 and materially addresses items 1 and 2.
Item 3 (`docs/guides/per-agent-models.md:141`) is real and
documenter-owned; v3 re-propose extends the pre_merge_condition list
to name it alongside `orchestrator/README.md:278`.

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
