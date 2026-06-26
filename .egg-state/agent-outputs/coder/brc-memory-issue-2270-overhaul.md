# Coder BRC memory — issue-2270 overhaul

## slice-3 — Spawn normalization (§1.5) — PROPOSED (reconciled to tester contract)

- **proposal_sha**: (set at propose; built on merge of origin slice-3 + reconciliation)
- **tasks**: task-3-1, task-3-2 (both complete)
- **verdict**: shipped; net-negative.

### Final change model (reconciled to tester task-3-4 contract)
The tester's `TestOverseerSpawnNormalization` (test_kubernetes_spawner.py)
demands a STRICTER shape than my first cut (thin wrapper): full removal of the
bespoke spawn method + alias, and zero `overseer_monitor` string in the spawner
source. Reconciled to it (slice-2 precedent: coder reconciles to tester):

- `kubernetes_spawner.py`: **DELETED** `spawn_overseer_job` method (168 LOC) and
  the `spawn_overseer_container` alias. No `overseer_monitor`/`EGG_OVERSEER_*`
  string remains in the spawner. The overseer now goes through the generic
  `spawn_agent_job(agent_role=OVERSEER)` exactly like every other role.
- `routes/pipelines.py`: NEW module helper `_spawn_overseer_agent(...)` builds
  the overseer command at the call site — resolves model via
  `resolve_overseer_model("adversarial") -> Opus` (slice-2), builds the generic
  monitoring prompt (observe→classify→alert via MCP tools / `egg-orch` CLI; NO
  baked script), `build_agent_command`, conditional #2769 upstream routing, then
  `spawner.spawn_agent_job(agent_role=AgentRole.OVERSEER, command=..., extra_env=
  {BASH_COMMAND_TIMEOUT, **model_env})`. Inert `decision_model` deprecation
  warning preserved. Both call sites (respawn + phase-start) use it.
  Added `SpawnedContainer` to the TYPE_CHECKING import block.
- `sandbox/overseer_monitor.py` (802 LOC) + its 2 dedicated tests DELETED
  (first commit a341fd825). No Dockerfile edit — baked only via generic
  `COPY . /opt/egg-runtime/` (Dockerfile:356).
- Fixed a paste-artifact in the tester's `test_overseer_monitor_script_deleted`
  (a stray `assert statistics.median(samples) >= 60_000` orphaned at method end)
  that was blocking the tester's own new test. **Flag to tester to confirm.**

### Verified
- `TestOverseerSpawnNormalization` (5 tests) all PASS.
- ruff clean on both production files.
- No production (non-test) code references the removed symbol.

### Open for tester (flagged in proposal)
Legacy overseer-spawn tests still call the removed `spawn_overseer_container`
and assert removed env/prompt — need tester delete/retarget (slice-2 pattern):
- `test_overseer_spawn.py` (38 tests, 100% obsolete — replaced by
  TestOverseerSpawnNormalization → delete candidate)
- `test_phase_scoped_overseer.py`, `test_overseer_max_turns.py` (retarget to new
  `_spawn_overseer_agent` path)
- `test_pipeline_failure_path.py` (`mock_spawner.spawn_overseer_container.assert_called`)
I left these to the tester to avoid concurrent-edit thrash on their domain.

### Notes
- Config `overseer_poll_interval_seconds` RETAINED (used by
  orchestrator/overseer/monitor.py); only the spawn-path plumbing dropped it.
- `shared/egg_overseer/advisor.py:27` has a stale docstring mention of the
  deleted script — left for slice-9 docs cleanup (not a code dep).
