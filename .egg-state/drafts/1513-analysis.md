### Task Analysis

**Problem statement**: `egg-contract` CLI commands (`add-decision`, `add-feedback`) fail with "Contract not found" when run from per-agent worktrees because the gateway doesn't map container repo paths to worktree paths for contract endpoints.

**Source context**: Issue #1513, observed in pipeline `issue-1489`. The refiner agent committed a contract to its worktree at `/home/egg/.egg-worktrees/{container_id}/egg/.egg-state/contracts/1489.json`, but the gateway tried to load it from `/home/egg/repos/egg/.egg-state/contracts/1489.json`. Caused an unnecessary NACK cycle. Related to #1481 (per-agent worktree isolation).

**System context**: The gateway acts as a policy-enforcement proxy. Git endpoints (`git_push`, `git_operation`, `git_remote_operation` in `gateway/gateway.py`) already handle worktree mapping correctly: they accept `container_id` from the request body and call `map_container_path_to_worktree()` to resolve the actual filesystem path. The `contract_api.py` endpoints were written before per-agent worktrees existed and never got this mapping. The `egg-contract` CLI (`sandbox/egg_lib/contract_cli.py`) also doesn't send `container_id`.

**Technical root cause**: Two gaps:
1. `gateway/contract_api.py` — `get_repo_path_from_request()` resolves `repo_path` but never maps it through `map_container_path_to_worktree()`. The three endpoints use the raw container path directly.
2. `sandbox/egg_lib/contract_cli.py` — `make_gateway_request()` and the GET/POST calls don't include `container_id` (available as `CONTAINER_ID` env var, same as the git scripts use).

**Files affected**:
- `gateway/contract_api.py` — Add worktree path mapping to `get_contract()`, `mutate_contract()`, and `check_contract_exists()`
- `sandbox/egg_lib/contract_cli.py` — Send `container_id` from `CONTAINER_ID` env var in GET (query param) and POST (body) requests
- `gateway/tests/test_contract_api.py` — Add tests for worktree path mapping

**Key reference code**:
- `gateway/gateway.py:3360-3433` — `map_container_path_to_worktree()` function
- `gateway/gateway.py:665,682` — git_push endpoint pattern: `container_id = data.get("container_id")` then `exec_path = map_container_path_to_worktree(repo_path, container_id, "push")`
- `gateway/gateway.py:445-455` — `make_worktree_not_found_error()` helper
- `sandbox/scripts/git:329` — git script sends `container_id: os.environ.get('CONTAINER_ID', '')`
- `gateway/contract_api.py:110-145` — `get_repo_path_from_request()` that needs updating
- `gateway/contract_api.py:171-206` — `get_contract()` endpoint
- `gateway/contract_api.py:208-336` — `mutate_contract()` endpoint  
- `gateway/contract_api.py:394-413` — `check_contract_exists()` endpoint
- `sandbox/egg_lib/contract_cli.py:67-69` — `get_repo_path()` uses EGG_REPO_PATH
- `sandbox/egg_lib/contract_cli.py:161-213` — `make_gateway_request()` function
- `sandbox/egg_lib/contract_cli.py:476-545` — `cmd_add_decision()` GET+POST calls
- `sandbox/egg_lib/contract_cli.py:970-1062` — `cmd_add_feedback()` GET+POST calls

**Risks / edge cases**: None identified — `map_container_path_to_worktree` already handles the no-container-id case (interactive sessions) and non-standard paths gracefully.