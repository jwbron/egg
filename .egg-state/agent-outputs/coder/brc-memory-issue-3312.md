# coder BRC memory — issue #3312, slice-12

## Verdict: PROPOSED (gateway/worktree_manager.py decomposition)
- Slice-12 target: gateway/worktree_manager.py (2,507 lines, OVER 100KB byte cap)
  -> sub-package gateway/worktree_manager/ (method-modules-on-class, §(c)).
- Branch: egg/issue-3312-slice-12-coder/work (base = slice-11 landed @ 60a61ac01).

## What landed (coder-owned commits)
1. a745e1ea4 step-0 baseline: pure git mv worktree_manager.py -> worktree_manager/__init__.py (byte-identical).
2. 03167d2b6 decompose: 6 cluster submodules + barrel.
3. 593bd5cf5 conftest: load as package via spec_from_file_location + submodule_search_locations (mirrors git_client slice-11; single-file loader can't exec a package).
4. 197298d9b drop allowlist entry (scripts/file-size-allowlist.yaml).
5. 4240b7b1d gateway/Dockerfile: explicit `COPY gateway/worktree_manager/ ./worktree_manager/` (non-recursive *.py glob misses the dir).
- gateway/CLAUDE.md worktree_manager/ seam-table subsection: DOCUMENTER-OWNED (coder is role-blocked from
  gateway/CLAUDE.md by shared/egg_restrictions/patterns.py; push denied -> dropped from coder proposal,
  alternative_role=documenter). The drafted table content is in this slice's coder work; documenter adds it.

## Cluster layout (class-dominated; method-modules-on-class)
Largest submodule _create.py = 985 lines / 40KB — all under 1,500-line / 100KB cap.
- _common (138)  WORKTREE_BASE_DIR, REPOS_BASE_DIR, WorktreeInfo, WorktreeRemovalResult, validate_identifier, validate_branch_ref, _tracking_refspec, _format_bytes, logger. NO intra-pkg deps -> cycle-free base everyone imports.
- _create (985)  resolve_default_branch, create_worktree, create_phase_worktree, _configure_push_upstream, _git_credential_env, _resolve_assigned_fork_point, _reset_reused_worktree_to_safe_ref, _run_git_worktree_add
- _remove (365)  remove_worktree, cleanup_phase_worktrees, cleanup_clean_worktree, _delete_worktree_branch
- _cleanup (605) cleanup_orphaned_worktrees, prune_stale_worktrees, git_worktree_prune_all, list_orphan_worktree_dirs, cleanup_orphaned_pack_files, cleanup_stale_pipeline_worktrees, _is_pipeline_anchored (staticmethod)
- _query (200)   lookup_worktree, list_worktrees, list_worktrees_for_pipeline
- _fsutil (202)  _get_repo_lock, _chown_single, _chown_recursive, _find_worktree_git_dir, get_worktree_paths
- __init__ (259) WorktreeManager skeleton (__init__ + 28 method bindings), get_active_docker_containers, startup_cleanup, __all__, patch seams.

## Correctness posture (pure refactor proof)
- AST-equivalence: 26/28 method bodies byte-for-byte identical (docstrings excluded) to pre-split file.
- The ONLY 2 deltas are the patch-seam indirection (verified: equivalent after un-_barrel()):
  * _git_credential_env: get_token_for_repo/create_credential_helper/cleanup_credential_helper -> _barrel().<X>
  * cleanup_stale_pipeline_worktrees: get_active_docker_containers() -> _barrel().get_active_docker_containers()
- `_barrel()` = `sys.modules[__package__]` so REBIND patches (patch("worktree_manager.get_token_for_repo"),
  patch("worktree_manager.get_active_docker_containers")) resolve at call time after the split.
  Verified live: patched get_token_for_repo IS invoked through _barrel().
- ATTRIBUTE patches (worktree_manager.subprocess.run, worktree_manager.time.sleep) work via shared module
  singletons; barrel keeps `import time  # noqa: F401` purely as that patch-target seam.
- _is_pipeline_anchored re-bound as `staticmethod(_cleanup._is_pipeline_anchored)` (verified static).
- Barrel re-exports full public surface (WorktreeManager, WorktreeInfo, WorktreeRemovalResult,
  validate_identifier, validate_branch_ref, REPOS_BASE_DIR, WORKTREE_BASE_DIR, get_active_docker_containers,
  startup_cleanup) + credential seams; gateway.py + session_manager.py importers preserved.
- Dual imports: `..git_client` (packaged) / `git_client` (flat container + conftest) for credential helpers + git_cmd.
- shared/ sys.path bootstrap gained one `.parent` (one dir deeper); stays the dead-but-faithful no-op the
  original had (computed path doesn't exist; egg_logging resolves via PYTHONPATH).

## Tests / lint
- ruff check + ruff format --check: All checks passed (7 files). check-file-sizes.py exit 0
  (no worktree_manager warning, no stale-entry error).
- Gateway suite: 3295 passed, 15 skipped. Cross-tree: orchestrator/tests/test_worktree_hitl.py 15 passed.
  Container-layout import smoke (reproduced Dockerfile COPY: gateway/*.py + worktree_manager/ + git_client/):
  `import worktree_manager` OK.
- Environmental failures only (PRE-EXISTING, NOT regressions):
  * git init blocked in container -> test_worktree_manager real-git tests (1 fail + 6 errors:
    TestWorktreeManagerDockerGitDir/TestNarrowRefspecMirror/TestAssignedBranchForkPoint),
    test_git_client_base_branch (3).
  * egress proxy HTTP 403 -> test_gateway TestHealthCheckServer (3).
  * pytest 9.1.1 / no-venv caplog double-emit -> test_partition_files_by_role::test_unknown_role_is_deny_by_default.
    PROVEN pre-existing via A/B: fails IDENTICALLY on the pre-slice flat tree (HEAD~1). Unrelated to
    worktree_manager (neither it nor git_client import agent_restrictions). CI's pinned venv runs the
    correct pytest where it passes.
- No .venv locally (uv sync needs network -> cert error); verified via system ruff 0.15.20 + targeted
  pytest. CI `make test-all` runs the full pinned suite green.

## Anticipated reviewer questions
- "Why _barrel() instead of direct import?" REBIND patch semantics: `from .git_client import get_token_for_repo`
  in the submodule would make the tests' `patch("worktree_manager.get_token_for_repo")` a no-op. Reading off
  the barrel at call time preserves the seam. Only the 2 methods that touch rebind-patched globals use it.
- "make test-all not run locally": no .venv (uv sync cert error) + container git-init block + egress 403.
  Verified via system-ruff + 3295-pass gateway suite + AST-equivalence + container-layout import smoke.
- "test_partition_files_by_role failing": pre-existing pytest-version caplog artifact, proven by A/B on HEAD~1.
