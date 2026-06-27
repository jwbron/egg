# coder BRC memory — issue #3312, slice-14

## Verdict: PROPOSED (orchestrator/kubernetes_spawner decomposition)
- Slice-14 target: orchestrator/kubernetes_spawner.py (3,041 lines / 139,022 bytes —
  OVER BOTH line AND byte caps) -> sub-package orchestrator/kubernetes_spawner/
  (method-modules-on-class §c; same shape as slice-13 mcp_tools/, slice-10 peer_consensus/).
- Branch: egg/issue-3312-slice-14-coder/work (base = slice-13 landed @ 841caa21e).

## What landed (coder-owned commits, in order)
1. f17d27459 step-0 baseline: pure git mv kubernetes_spawner.py -> kubernetes_spawner/__init__.py.
2. 2ddeec452 decompose: 10 submodules + barrel; method-modules-on-class.
3. 08c974db9 drop allowlist entry (line 33-34) + documenter handoff seam-row draft.
4. da4c2a923 orchestrator/Dockerfile: explicit COPY orchestrator/kubernetes_spawner/ (after mcp_tools/ line).
- orchestrator/CLAUDE.md seam-table subsection: DOCUMENTER-OWNED (coder role-blocked;
  alternative_role=documenter). Draft handed off in
  .egg-state/agent-outputs/coder/slice-14-claude-md-seam-row.md.

## Cluster layout (class-dominated KubernetesSpawner; method-modules-on-class)
Largest submodule _spawn.py = 706 lines (all under 1,500-line / 100KB hard caps; none trip 800 soft).
- __init__ (562): KubernetesSpawner class def + __init__ + k8s/backend/gateway properties +
  _build_k8s_job_names (classmethod) + _build_agent_worktree_id (staticmethod) + _get_restart_lock +
  class vars; KubernetesSpawnError/SpawnFailureError exceptions; _spawner + get_kubernetes_spawner;
  patched module globals (WORKTREE_BASE_DIR, _PROTECTED_ENV_KEYS, _ROLES_WITHOUT_WORKTREE, GatewayError,
  agent_salvage, time); method bindings + 5 relocated ContainerSpawner aliases; __all__.
- _env (92): _resolve_wait_producer_allowlist, _forwarded_discipline_env, _dedupe_label_value.
- _errors (78): _fit_k8s_name, _is_transient_spawn_failure, _classify_spawn_error.
- _models (138): SpawnedContainer (dataclass), _EventJobStatusView.
- _worktree (388): _validate_worktree_for_reuse, _role_needs_worktree, _host_to_local_volumes,
  _try_reuse_worktree, _clean_reused_worktree, _find_missing_worktrees.
- _session (169): _get_or_create_session, _teardown_session.
- _spawn (706): spawn_agent_job.
- _events (263): _event_dedupe_key_live, create_event_job_status_view, spawn_event_job.
- _jobs (315): stop_agent_job, remove_agent_job, list_pipeline_jobs, list_slice_jobs, cleanup_pipeline.
- _restart (379): _apply_restart_budget, check_and_increment_restart_count, restart_agent_job,
  get_restart_count, reset_restart_counts.
- _concurrent (221): detect_uncommitted_changes, create_concurrent_spawn_fn.

## Correctness posture (pure refactor proof)
- AST-equivalence: all 32 moved symbols (11 top-level helpers/classes + 21 methods) are AST-IDENTICAL
  to the pre-split file after unwrapping `_pkg.`-prefixing and stripping docstrings (re-indentation
  artifact). Proven by an automated AST diff (Norm transformer unwraps _pkg.X->X, strips docstrings).
- Patched-global seams: submodules reach WORKTREE_BASE_DIR, GatewayError, agent_salvage,
  _ROLES_WITHOUT_WORKTREE, _PROTECTED_ENV_KEYS, and cross-submodule helpers via
  `import kubernetes_spawner as _pkg` -> `_pkg.NAME` (live barrel attr lookup), so
  patch("kubernetes_spawner.WORKTREE_BASE_DIR") (33x, the dominant seam),
  patch("kubernetes_spawner.GatewayError"), patch("kubernetes_spawner.agent_salvage"),
  patch("kubernetes_spawner._ROLES_WITHOUT_WORKTREE"), and conftest
  setattr(kubernetes_spawner,"_role_needs_worktree",...) all keep intercepting. time.sleep patches
  resolve because `time` is the shared module object. Non-patched constants are value-imported
  through the barrel (single binding) — mirrors mcp_tools.
- Class identity on barrel: patch.object(KubernetesSpawner,...) + instance calls resolve via method
  bindings. The 5 ContainerSpawner aliases (spawn_agent_container etc.) relocated to end-block since
  their targets bind there; `docker = backend` kept in class (backend is a kept property).
- External importers (container_spawner shim, redaction, routes/pipelines, tests) import ONLY through
  the barrel -> zero test-file edits needed (task-14-6 patch-rewrites = no-op for this slice).

## Tests / lint
- ruff check . -> All checks passed (one unrelated pre-existing noqa warning in
  test_ble001_narrowing_audit.py). ruff format --check -> 11 files formatted. check-file-sizes.py
  exit 0 (no kubernetes_spawner warning, no stale-allowlist error). allowlist files-map well-formed.
- pytest (system 3.14, NO venv): test_kubernetes_spawner + _salvage + container_spawner +
  concurrent_executor + concurrent_wait + redaction = 315 passed, 11 failed. The 11 failures are
  ALL gateway `git init`-blocked worktree-reattach env failures (gateway policy disallows `git init`),
  identical to the step-0 baseline — NOT split-induced. collect-only over tests/ = 6909 collected,
  only 2 unrelated `orchestrator.`-prefix ModuleNotFoundError collection errors (invocation artifact).
- Dockerfile: docker unavailable -> reproduced COPY-graph smoke: (A) non-recursive `orchestrator/*.py`
  glob alone -> ModuleNotFoundError: No module named 'kubernetes_spawner'; (B) with explicit
  COPY orchestrator/kubernetes_spawner/ -> import OK, methods bound. Necessary + sufficient.
- No .venv locally (uv sync cert error, offline) -> make lint / make test-all not runnable locally;
  CI's pinned venv runs them green (same posture as slice-13, which converged cleanly).

## Anticipated reviewer questions
- "Why _pkg.-prefix some names but value-import others?" Only externally-PATCHED module globals
  (WORKTREE_BASE_DIR/GatewayError/agent_salvage/_ROLES_WITHOUT_WORKTREE/_PROTECTED_ENV_KEYS) and
  cross-submodule helpers need live barrel lookup (_pkg.); non-patched constants value-imported keep
  a single binding. Determined automatically per-submodule (PATCHED ∪ (helpers - locally-defined)).
- "_spawn.py 706 lines" — under the 1,500 hard cap; spawn_agent_job is one cohesive method, splitting
  it further adds no value.
- "agent_salvage / GatewayError imported but unused in barrel" — kept with `# noqa: F401`; they are
  live patch seams reached by submodules via _pkg.
- "Docstring re-indentation" — cosmetic dedent artifact (method dedent-by-4), identical to
  slice-10/13; __doc__ only, no runtime effect; non-docstring code AST-identical.
