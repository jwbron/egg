### Task Analysis

**Problem statement**: The DevserverManager component manages Docker Compose-based deployment validation stacks but has never been used. With the full Docker removal in #1553 (Kubernetes migration), it will be left without a runtime and should be removed entirely.

**Source context**: Issue #1558 is a follow-up from #1553 (Kubernetes migration). No known users of this feature. Docker Compose is being fully removed in #1553.

**Workarounds**: None needed — the feature was never used.

**System context**: DevserverManager (`orchestrator/devserver.py`) manages a Docker Compose lifecycle for deployment validation during the check phase. It extracts compose configs from git, generates override YAML with read-only mounts and resource limits, creates air-gapped Docker networks, starts/stops stacks, and attaches sandbox checkers. It's exposed via REST endpoints in `orchestrator/routes/checks.py` (start/status/teardown). The `teardown_devserver()` function is called from `orchestrator/routes/phases.py` during phase transitions and pipeline completion to clean up any active devserver. Constants live in `shared/egg_config/constants.py` and deployment config models in `shared/egg_contracts/deployment.py`.

**Technical root cause**: Dead code — the feature was never activated and its runtime (Docker Compose) is being removed.

**Files affected**:
- `orchestrator/devserver.py` — delete entirely
- `orchestrator/tests/test_devserver.py` — delete entirely
- `orchestrator/routes/checks.py` — delete entirely
- `orchestrator/tests/test_routes_checks.py` — delete entirely
- `orchestrator/api.py` — remove `checks_bp` import and registration (2 blocks)
- `orchestrator/routes/phases.py` — remove `teardown_devserver` import and 3 call sites
- `shared/egg_config/constants.py` — remove `DEVSERVER_*` and `EGG_CHECK_NETWORK_PREFIX` constants + `__all__` entries
- `shared/egg_contracts/deployment.py` — delete entirely (only imported by devserver code)
- `tests/shared/egg_contracts/test_deployment_config.py` — delete entirely
- `integration_tests/deployment_validation/` — delete directory

**Risks / edge cases**: None identified — `egg_contracts.deployment` is only imported by devserver-related files, and `teardown_devserver()` is a no-op when no devserver is active.