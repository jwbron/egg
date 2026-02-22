# Analysis: Wire Full-Stack Integration Tests into CI and SDLC Pipeline

> Issue: #647 | Phase: refine

## Problem Statement

The egg project has 91+ integration tests across 8 files in `integration_tests/local_pipeline/` covering pipeline CRUD, multi-agent orchestration, HITL decision flows, container failure/recovery, signal handling, worktree integration, and more. Full test infrastructure exists — compose files, mock sandbox, session-scoped fixtures, and a `make test-integration` target. However, **none of these tests run automatically on PRs or during the SDLC pipeline's own check phase**.

The desired outcome: integration tests gate PRs in GitHub Actions CI, and egg's own tester agent can run full-stack integration tests against its own changes during the SDLC pipeline.

## Current Behavior

### CI Workflows — Nothing Gates PRs

The three test/lint workflows are `workflow_call`-only with no callers on PR events:

| Workflow | Triggers | Status |
|----------|----------|--------|
| `lint.yml` | `workflow_call`, `workflow_dispatch` | **Never runs on PRs** |
| `test.yml` | `workflow_call`, `workflow_dispatch` | **Never runs on PRs** |
| `test-integration.yml` | `workflow_call`, `workflow_dispatch` | **Never runs on PRs** |

The only PR-triggered workflows are code review (`on-pull-request.yml`), contract verification (`on-pull-request-contract-verify.yml`), and the action test (`test-action.yml`, path-filtered to `action/**`).

The autofix workflow (`on-check-failure.yml`) listens for completions of workflows named "Lint" and "Test" — but since those never trigger, autofix never fires either.

**Related work**: Issue #632 (audit tests) identified this same gap. PR #739 (merged 2026-02-15) added `orchestrator/tests/` to the `test.yml` pytest command and unified pytest config, but did **not** add `pull_request` triggers to any workflow. The core problem remains unresolved.

### `test-integration.yml` — Missing Orchestrator Build

The workflow only builds the gateway image:

```yaml
- name: Build gateway container
  run: docker build -t egg-gateway -f gateway/Dockerfile .
```

But the local pipeline tests require **both** `egg-gateway` and `egg-orchestrator` images. The compose stack (`integration_tests/local_pipeline/docker-compose.yml`) builds both via `docker compose up -d --build`, so the test fixture handles this today — but the CI workflow should build both explicitly for caching and clear failure reporting.

Additionally, the workflow runs `pytest integration_tests -v -m "integration or security"`, but `integration_tests/local_pipeline/conftest.py` builds a mock sandbox image (`docker build -t mock-sandbox:latest`) as part of the session fixture. This should work on `ubuntu-latest` (Docker is available) but has never been validated in CI.

### No Docker Access in SDLC Pipeline Sandbox

When egg's tester agent runs inside a sandbox container, it has **no Docker socket access**. The sandbox is intentionally untrusted and cannot manage containers. Running full-stack integration tests (which spawn gateway + orchestrator + mock-sandbox containers via docker-compose) is impossible from within the sandbox.

The orchestrator has Docker socket access (`/var/run/docker.sock` mounted), but the sandbox does not. This is a fundamental architectural constraint — the sandbox should never get Docker access.

## Constraints

- **Sandbox trust model**: The sandbox container must never receive Docker socket access. All Docker operations must be orchestrator-driven.
- **`workflow_call` compatibility**: `lint.yml` and `test.yml` must remain callable as reusable workflows (used by `on-check-failure.yml` autofix, which listens for "Lint" and "Test" workflow completions).
- **CI runner environment**: GitHub Actions `ubuntu-latest` has Docker natively available. Docker Compose is also available. No special setup needed for container-based tests.
- **Integration test duration**: The local pipeline tests build images, start compose stacks, wait for health checks, run tests, and tear down. This adds significant CI time (estimated 5-10 minutes) vs unit tests (~1-2 minutes).
- **Issue #645 (DinD deployment validation)**: Closed/completed. The orchestrator already has a `DevserverManager` (`orchestrator/devserver.py`, 1028 lines) for Docker-in-Docker operations. This infrastructure could be extended for integration test execution.
- **Network isolation**: Test compose stacks use isolated subnets (172.40.x/172.41.x) to avoid collision with production networks (172.32.x/172.33.x).

## Options Considered

### Option A: Add PR Triggers to Existing Workflows

**Approach**: Add `on: pull_request` triggers directly to `lint.yml`, `test.yml`, and `test-integration.yml`. Fix `test-integration.yml` to build both gateway and orchestrator images. Integration tests run directly on the GitHub Actions runner (not inside a sandbox).

**Pros**:
- Simplest change — minimal new files, leverages existing infrastructure
- `workflow_call` + `pull_request` triggers coexist in GitHub Actions
- `on-check-failure.yml` autofix works automatically (listens for "Lint" and "Test" names)
- Docker is natively available on `ubuntu-latest` — no DinD needed for CI
- Local pipeline test fixture already handles compose stack lifecycle

**Cons**:
- If another workflow ever calls them via `workflow_call` on PRs, they'd run twice
- Does not address Phase 2 (SDLC pipeline self-testing)
- Integration tests add CI time (~5-10 minutes per PR)

### Option B: Create a Caller `ci.yml` Workflow

**Approach**: Create a new `ci.yml` workflow triggered on `pull_request` that calls `lint.yml`, `test.yml`, and `test-integration.yml` via `workflow_call`.

**Pros**:
- Clean separation — reusable workflows remain purely reusable
- Single place to manage what runs on PRs
- Easy to add/remove checks from the PR gate

**Cons**:
- `on-check-failure.yml` listens for workflow names "Lint" and "Test" — called workflows run as jobs within the caller, so the event names would be "CI" not "Lint"/"Test", breaking autofix
- Adds indirection for a marginal organizational benefit
- More complex to reason about

### Option C: Orchestrator-Managed DinD for SDLC Self-Testing

**Approach**: For the SDLC pipeline use case (egg testing itself), the orchestrator spawns a DinD sidecar container (`docker:27-dind-rootless`). The tester agent's sandbox connects to the DinD daemon via `DOCKER_HOST=tcp://dind:2375` and runs integration tests against a nested test stack.

**Pros**:
- Enables full-stack integration tests from within the SDLC pipeline
- Reuses existing orchestrator container management infrastructure
- DinD sidecar is managed by the trusted orchestrator, not the sandbox
- Nested test stack is fully isolated from production

**Cons**:
- Significant complexity: orchestrator must manage DinD lifecycle, pre-load images, manage networking
- DinD adds latency (nested Docker daemon startup, image loading)
- Requires `--privileged` or device access for DinD container
- May require custom conftest fixture to detect DinD environment and adjust accordingly
- Not needed for Phase 1 (CI gating) — only for self-testing in the pipeline

### Option D: Orchestrator Runs Tests Directly (No Sandbox Involvement)

**Approach**: The orchestrator itself runs integration tests — it already has Docker socket access and can manage compose stacks. The tester agent signals the orchestrator to run tests, and the orchestrator executes pytest directly.

**Pros**:
- No DinD complexity
- Orchestrator already has all necessary access

**Cons**:
- Violates the trust model: test execution (which may run arbitrary code from agent changes) should not happen in the trusted orchestrator
- Test failures or resource leaks could destabilize the orchestrator
- Tightly couples test execution to orchestrator internals

## Recommended Approach

**Phase 1 (CI gating): Option A** — Add `pull_request` triggers directly to `lint.yml`, `test.yml`, and `test-integration.yml`. This is the simplest path to get tests gating PRs. Fix `test-integration.yml` to build both images. This addresses the most critical gap (code merging without tests).

**Phase 2 (SDLC self-testing): Option C** — Orchestrator-managed DinD for the tester agent. This is the only approach that preserves the trust model while enabling full-stack testing from within the pipeline. The `DevserverManager` infrastructure from #645 provides a foundation for managing the DinD sidecar lifecycle.

Option B is rejected because it breaks the autofix workflow naming dependency. Option D is rejected because it violates the orchestrator trust model.

## Open Questions

1. **Should integration tests be a required check for PR merging?** They add significant CI time. Making them required blocks PRs on Docker-based test failures. An alternative is to run them but not gate merging initially, promoting to required once stable. This requires human input on the tradeoff between safety and velocity.

2. **Should lint and test failures trigger the autofix workflow for integration test failures too?** The `on-check-failure.yml` currently watches for "Lint" and "Test" completions. Should it also watch for "Integration Tests"? Integration test failures may not be auto-fixable.

3. **Phase 2 priority**: Is SDLC self-testing (Phase 2) a hard requirement for this issue, or can it be deferred? Phase 1 alone provides substantial value by gating PRs with tests. Phase 2 is significantly more complex and could be a follow-up issue.

---

*Authored-by: egg*

<!-- yaml
# metadata
complexity_tier: high
parallel_phases: true
-->
