# Coder BRC memory — issue-3312-v2, slice-1 (decompose orchestrator/models.py, closes #3450)

## Change model (CLAIM — verify against live git log)
- **Pattern:** domain-split (data-dominated pydantic-models module), NOT method-modules-on-class.
- **Two commits:** `8e8ed492e` pure `git mv` baseline (models.py → models/__init__.py, byte-identical),
  then `8a6af6ae6` extraction into 6 domain submodules + barrel + Dockerfile COPY + allowlist drop + CLAUDE.md seam.
- **Submodules** (all under both caps; largest `_config.py` 593 lines / 27KB):
  `_enums` (status StrEnums + LIVE_POD_STATUSES + AgentRole re-export),
  `_decisions` (HITLDecision/OperatorDirective/IterationSummary),
  `_execution` (ReviewVerdict/AggregatedReviewResult/ContainerInfo/AgentExecution/CycleTiming/AgentExitInfo/PhaseExecution/_REMOVED_ROLE_MIGRATION),
  `_config` (PipelineConfig + resolve_consensus_timeout_minutes + PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN),
  `_pipeline` (RepoSpec/Pipeline/resolve_slice_repo),
  `_events` (PipelineEvent/ProgressEvent).
- **Import DAG (no cycles):** _enums → _decisions → {_config, _execution} → _pipeline → _events. Siblings import relative; external deps absolute (matches state_store).
- **Barrel** = stable public API: explicit per-symbol re-exports; also re-exports sibling-pkg symbols consumers pull through models: `PipelinePhase`, `Slice` (egg_contracts.models), `AgentRole` (egg_contracts.agent_roles), plus OVERSEER_TIER_MODELS / SLICE_ID_PATTERN. `__all__` lists 31 symbols.
- **No `patch("models.<name>")` module-global seams exist** (audited) → submodules import collaborators directly, no `import models as _pkg` indirection needed.
- **Python 3.14.6:** annotations lazy (PEP 649/749) → the pre-split forward ref (resolve_consensus_timeout_minutes → PipelineConfig) is fine; kept intra-module in _config regardless.

## Audit (task-1-1) — CLAIM
- ~197 files reference the module; dominant style bare `from models import X`. `PipelinePhase as PipelineModelsPhase` (routes loop) is load-bearing.
- Edge symbols (resolve_consensus_timeout_minutes, ReviewerType, AgentExitInfo, _REMOVED_ROLE_MIGRATION, OVERSEER_TIER_MODELS, SLICE_ID_PATTERN): 0 problematic external refs; re-exported anyway.
- No `__module__`-sensitive consumer.

## Verdict / verification (my proposal SHA 8a6af6ae6)
- **Repo-wide `pytest --collect-only`: 16,041 tests, 0 import errors** → every models importer resolves post-split.
- **orchestrator/tests/test_models.py: 124/124 pass.** ruff check + format clean. file-size ratchet exit 0 (models.py gone from allowlist; every submodule under caps).
- Full orchestrator suite: 7,471 passed; **143 non-passing are ALL sandbox env failures** — `git init not supported in the container` / CalledProcessError / k8s `list_namespaced_pod` unreachable — in git/worktree/session/k8s modules (gateway_client, agent_salvage, kubernetes_spawner, cli, reconcile*, slice_diff*, commit_statefiles*, ...). NONE reference models/import/attribute. Identical env class documented by prior landed slices.
- **NOT executable in this sandbox:** Dockerfile COPY smoke-check (`docker build` + `import models` in image) — no docker/network. COPY line added per exact established pattern (mirrors gateway_client etc.). Flag for reviewer_code if image-build verification is required.

## Open NACK responses
(none yet)
