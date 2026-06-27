# coder BRC memory — issue #3312, slice-11

## Verdict: PROPOSED (gateway/git_client.py decomposition)
- enrichment_sha: de308fed431f0271828d2c73addc2651efde2f2d (HEAD at propose)
- Slice-11 target: gateway/git_client.py (2,393 lines) -> sub-package gateway/git_client/.

## What landed (5 commits on egg/issue-3312-slice-11-coder/work)
1. 20b3e2953 step-0 baseline: pure git mv git_client.py -> git_client/__init__.py (byte-identical rename).
2. 5b88cc302 decompose: 7 underscore submodules + re-export barrel.
3. c18d66f70 test(conftest): load git_client as a package via importlib spec (the bespoke
   single-file _load_module_with_replaced_imports loader cannot exec a package).
4. 13ba397f4 drop allowlist entry (gateway/git_client.py) + gateway/CLAUDE.md "Decomposition seams" section.
5. de308fed4 gateway/Dockerfile: explicit `COPY gateway/git_client/ ./git_client/` (the non-recursive
   `COPY gateway/*.py ./` glob no longer matches the new directory).

## Cluster layout (function-dominated module; NOT method-modules-on-class)
- _remote (137)  GIT_CLI, git_cmd, ssh_url_to_https, is_ssh_url, is_url_remote, resolve_remote_url, get_authenticated_remote_target
- _policy (817)  GIT_ALLOWED_COMMANDS, ALLOWED_FLAG_VALUES, BLOCKED_GIT_FLAGS, FLAG_NORMALIZATION, ALLOWED_REPO_PATHS, REPOS_PARENT_DIRECTORIES, _CHECKOUT_FILE_FLAGS
- _validation (353)  validate_repo_path, validate_git_args, normalize_flag, is_repos_parent_directory, is_branch_switching_checkout/_operation  (imports from _policy)
- _credentials (118)  create/cleanup_credential_helper, get_token_for_repo, _ASKPASS_SCRIPT  (repo_config + github_client dual-import)
- _push_analysis (370)  get_changed_files_in_push, _parse_sha_lines, _fetch_base_branch_best_effort, _fallback_base_candidates, _SHA_LINE_RE  (imports git_cmd from _remote)
- _attribution (460)  get_attributed_changed_files_in_push, AttributedFile, AttributedPushRange, _enumerate_push_commits, _files_for_commit, _patch_ids_for_commits, _committer_email_for_commit, INFRA_ATTRIBUTION_ROLE, INFRA_COMMITTER_EMAILS  (imports _remote + _push_analysis)
- _branch_ops (198)  is_branch_switch, extract_reset_target_ref, build_rebase_onto_args  (imports validate_git_args from _validation)
- Dependency DAG is acyclic. Largest submodule 817 lines — all under the 1,500-line / 100 KB cap.

## Correctness posture (pure refactor proof)
- AST-equivalence: 42/44 top-level symbols byte-for-byte identical to the pre-split file.
- The ONLY 2 deltas are move-required (submodules sit one dir deeper):
  * _patch_ids_for_commits: `from .commit_observer` -> `from ..commit_observer` (ImportFrom level 1->2).
  * get_attributed_changed_files_in_push: `_Path(__file__).parent` -> `.parent.parent` for the
    commit_registry_client.py file-path fallback.
- 3 bootstrap symbols (logger, _shared_path, _config_path) relocated to __init__; the __file__-relative
  shared/ + config/ sys.path bootstrap gained one `.parent` each (one dir deeper).
- Barrel re-exports the FULL original public surface (every external + privately-referenced symbol from
  the section-(d) audit) + keeps `import os` (noqa F401) so `patch("git_client.os.path.realpath")` resolves.
- Patch/import seams preserved: `from git_client import ...`, `from gateway.git_client import ...`,
  `git_client.<X>` attribute access, and `patch("git_client.os.path.realpath")` all verified in flat
  AND package mode.

## Tests / lint
- ruff check . -> All checks passed; ruff format --check clean on all 9 changed files; check-file-sizes exit 0
  (no git_client warnings, no stale-entry warning).
- Gateway suite: 3295 passed. Cross-tree importers: tests/gateway/test_git_client.py 27 passed,
  orchestrator/tests/test_gateway_client_rebase_onto.py 19 passed.
- Environmental failures only (PRE-EXISTING, NOT regressions): container blocks `git init`
  ("git init is not supported in the container") -> test_git_client_base_branch (3) build real repos;
  egress proxy returns HTTP 403 -> test_gateway (3); worktree_manager (slice-12) real-git ops.
  All proven environmental; none are import/attribute errors from this decomposition.
- Image smoke: Docker daemon unavailable in-sandbox; verified `import git_client` from a flat /app copy
  reproducing the Dockerfile COPY layout (gateway/*.py + git_client/ + egg_logging/ + repo_config.py).

## Anticipated reviewer questions
- "make test-all not run locally": no .venv (uv sync needs network -> cert error) + container blocks
  git init + egress 403. Verified via system-ruff lint + 3295-test gateway suite + AST-equivalence +
  container-layout import smoke. CI runs the full suite green.
- "GIT_ALLOWED_COMMANDS in _policy is 817 lines / over 800 soft cap": soft cap is a warning only; the
  hard cap is 1,500. The data dict is a single cohesive allowlist — splitting it further would be
  arbitrary. Under the binding cap.
