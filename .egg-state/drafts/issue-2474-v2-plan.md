# Plan: Wire integration tests into PR CI; retire dead test tiers; expand coverage

> Issue: #2474 | Phase: plan | Pipeline: issue-2474-v2

## Approach

The refine analysis (`.egg-state/drafts/2474-analysis.md`) identified six
parts (A–F). All six parts ship together as a single stacked-PR train via
5 sequential slices. The slice DAG is **linear** — each slice has exactly
one parent, satisfying the forest constraint without needing
`serialized_chain_order`.

**Slicing rationale**: order each slice so the next can depend on its
predecessor's invariants:

1. **Slice 1 — Cleanup** (Parts B + C + D). Removes the dead docker-compose
   runtime branch, deletes `tests/functional/`, and retires the
   real-LLM e2e workflow. Lands first because it shrinks the test surface
   that everything downstream depends on. Pure deletions + a few conftest
   edits.
2. **Slice 2 — Promote `ScriptedProvider`** (Part E auxiliary). Moves
   `ScriptedProvider` to a public `shared/egg_harness/testing/scripted_provider.py`
   module so integration tests can import it.
3. **Slice 3 — New k3s integration tests** (Part E core). Adds 8 new
   regression-and-invariant tests under `integration_tests/regression/`.
   Depends on slice 2 and slice 1.
4. **Slice 4 — Wire integration tests into PR CI** (Part A). Adds a
   `workflow_call` of `test-integration.yml` from the `Test` workflow.
5. **Slice 5 — Documentation** (Part F + supporting docs). `CLAUDE.md`
   note + local-k3s recipe in `docs/guides/testing.md` (k3s only,
   no kind/minikube alternatives per operator direction).

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Slice 1 deletes 1,400+ LoC; reviewers may miss a transitive import | CI's unit suite catches Python-level dangling imports. |
| Slice 3 tests flake on cold k3s | Slice 4 pre-pulls images and adds `kubectl wait` timeouts. |
| Required-check flake on day 1 | decision-3 default keeps the check non-blocking until settle-in. |
| `ScriptedProvider` API drift breaks consumers | Slice 2 keeps the existing five test-file consumers passing as the canary. |
| #2449 (parallel issue) merges first | Part D becomes a no-op rebase. |

---

```yaml
# yaml-tasks
pr:
  title: |-
    Wire integration tests into PR CI; retire dead tiers; expand coverage
  description: |
    Wire `test-integration.yml` into PR CI; retire dead test tiers
    (Docker-compose runtime, tests/functional/, real-LLM e2e); expand
    integration coverage with 8 new k3s scenarios; document agent
    guidance. Multi-phase / multi-PR delivery via 5-slice stacked train.
  test_plan: |
    - Automated:
      * `make test-all` continues to pass on every slice.
      * `make lint` continues to pass.
      * `make test-integration` passes locally on k3s after slice 1 and after slice 3.
      * Slice 4 onwards: GitHub Actions integration job runs on each slice PR.
    - Manual:
      * Confirm new `Integration Tests / aggregate` check appears on a sample PR after slice 4.
      * Reviewer follows `docs/guides/testing.md` k3s-on-host recipe on a fresh laptop.
  manual_steps: |
    Pre-merge (slice 4): trigger `test-integration.yml` via workflow_dispatch on slice-4 branch; confirm green and within budget.
    Post-merge (slice 4 + 5): maintainer flips `Test / aggregate` to required in branch protection; close #2449 if absorbed.
slices:
  - id: 1
    name: |-
      Cleanup — k3s only, drop dead test tiers
    goal: |-
      Drop `EGG_RUNTIME=docker` branch from `integration_tests/conftest.py`;
      delete `tests/functional/`; retire `.github/workflows/test-e2e.yml`
      plus its test files; remove `functional`/`e2e`/`agent_flaky` markers
      and now-orphan `run_claude_structured()` / `assert_agent_verdict()` helpers.
    tasks:
      - id: TASK-1-1
        description: |-
          In `integration_tests/conftest.py` and `integration_tests/local_pipeline/conftest.py`,
          remove `_docker_egg_stack()` and the runtime-selection branch in
          `egg_stack`. Always call `_k8s_egg_stack()`; skip with clear
          message if `kubectl` unavailable. Remove `docker_available`
          import and call sites. Drop stale docker-compose comments.
          Flip default `EGG_RUNTIME` from "docker" to "kubernetes" or remove.
        acceptance: |-
          `grep -n "EGG_RUNTIME=docker\|_docker_egg_stack\|docker_available" integration_tests/conftest.py integration_tests/local_pipeline/conftest.py`
          returns no hits.
        role: coder
        files:
          - integration_tests/conftest.py
          - integration_tests/local_pipeline/conftest.py
      - id: TASK-1-2
        description: |-
          Delete entire `tests/functional/` directory (5 files). Remove
          `functional:` marker from `pyproject.toml`. Remove
          `tests/functional/conftest.py` and `integration_tests/docker-compose.yml`
          allowlist entries from `scripts/check-hardcoded-ports.py`.
        acceptance: |-
          `tests/functional/` no longer exists. `make test-all` passes.
          `grep -rn "tests.functional\|@pytest.mark.functional"` returns no hits.
        role: tester
        files:
          - tests/functional/conftest.py
          - tests/functional/test_git_wrappers.py
          - tests/functional/test_network_modes.py
          - tests/functional/test_session_lifecycle.py
          - tests/functional/__init__.py
          - pyproject.toml
          - scripts/check-hardcoded-ports.py
      - id: TASK-1-3
        description: |-
          Delete `.github/workflows/test-e2e.yml`,
          `integration_tests/test_e2e_workflow.py`,
          `integration_tests/test_agent_security_fuzz.py`, and
          `integration_tests/agent_findings.py`. Remove `e2e` and
          `agent_flaky` markers from `pyproject.toml`. Remove `test-e2e:`
          target from `Makefile`. Update `test-integration:` docstring.
        acceptance: |-
          The 4 files are gone. `make test-e2e` is no longer a valid target.
          `make help` does not advertise `test-e2e`. `make lint` and
          `make test-all` pass.
        role: coder
        files:
          - .github/workflows/test-e2e.yml
          - integration_tests/test_e2e_workflow.py
          - integration_tests/test_agent_security_fuzz.py
          - integration_tests/agent_findings.py
          - pyproject.toml
          - Makefile
      - id: TASK-1-4
        description: |-
          In `integration_tests/conftest.py`, remove `run_claude_structured()`,
          `assert_agent_verdict()`, the `infrastructure_failure` field on
          `AgentVerdict` dataclass, and orphan helpers used only by those.
        acceptance: |-
          `grep -rn "run_claude_structured\|assert_agent_verdict"` returns no hits.
          `make test-integration` and `make test-all` pass.
        role: coder
        files:
          - integration_tests/conftest.py
  - id: 2
    name: |-
      Promote ScriptedProvider to public testing API
    goal: |-
      Move `ScriptedProvider` and its private `_stream_events` helper to
      `shared/egg_harness/testing/scripted_provider.py` so slice-3 integration
      tests can hand each agent role a canned LLM trajectory.
    dependencies:
      - slice-1
    tasks:
      - id: TASK-2-1
        description: |-
          Create `shared/egg_harness/testing/__init__.py` and
          `shared/egg_harness/testing/scripted_provider.py` containing the
          class verbatim plus `_stream_events`.
        acceptance: |-
          `python -c "from shared.egg_harness.testing import ScriptedProvider; print(ScriptedProvider.__name__)"`
          prints `ScriptedProvider`. `make lint` passes.
        role: coder
        files:
          - shared/egg_harness/testing/__init__.py
          - shared/egg_harness/testing/scripted_provider.py
      - id: TASK-2-2
        description: |-
          In `shared/tests/test_egg_harness/test_integration.py`, replace
          inline ScriptedProvider class with re-export shim. Keep five call
          sites resolvable. `RecordingRegistry` stays inline.
        acceptance: |-
          File no longer contains `class ScriptedProvider` or `_stream_events`
          definitions. `make test` passes.
        role: tester
        files:
          - shared/tests/test_egg_harness/test_integration.py
      - id: TASK-2-3
        description: |-
          Add `shared/tests/test_egg_harness/test_scripted_provider.py` with
          two tests: import works, public API surface matches.
        acceptance: |-
          New test passes. Removing `scripted_provider.py` causes ImportError.
        role: tester
        files:
          - shared/tests/test_egg_harness/test_scripted_provider.py
  - id: 3
    name: |-
      Add k3s integration tests for recent regressions and invariants
    goal: |-
      Land 8 Part-E scenarios under `integration_tests/regression/`:
      regressions in #2428, #2429, #2420, #2430 plus 4 invariants.
    dependencies:
      - slice-2
    tasks:
      - id: TASK-3-1
        description: |-
          Create `integration_tests/regression/__init__.py` and `conftest.py`.
          Conftest re-exports parent k8s fixtures and adds `start_pipeline()`
          helper returning deterministic pipeline_id from test nodeid.
        acceptance: |-
          `make test-integration -m integration` includes new dir, "0 errors" on collection.
        role: tester
        files:
          - integration_tests/regression/__init__.py
          - integration_tests/regression/conftest.py
      - id: TASK-3-2
        description: |-
          Add `test_slice_branch_env.py` covering #2428. Spin 2-slice DAG;
          assert each slice coder pod's `EGG_BRANCH` matches its slice ref
          via `kubectl get pod -o jsonpath`.
        acceptance: |-
          Test passes on `main`. Reverting #2428 fix causes failure with clear assertion.
        role: tester
        files:
          - integration_tests/regression/test_slice_branch_env.py
      - id: TASK-3-3
        description: |-
          Add `test_unpushed_commit_salvage.py` covering #2429. Trigger
          gateway push rejection by attempting push outside role allowlist
          (no test backdoor). Assert recovery branch ref appears.
        acceptance: |-
          Test passes on `main`. Reverting salvage code causes "recovery ref not found".
        role: tester
        files:
          - integration_tests/regression/test_unpushed_commit_salvage.py
      - id: TASK-3-4
        description: |-
          Add `test_live_pod_guard.py` covering #2420. Start pipeline, wait
          for slice pods Running, call `start_pipeline` again WITHOUT force=true;
          assert refused. Retry with force=true; assert new pipeline replaces old.
        acceptance: |-
          Test passes on `main`. Reverting #2420 makes second start succeed.
        role: tester
        files:
          - integration_tests/regression/test_live_pod_guard.py
      - id: TASK-3-5
        description: |-
          Add `test_hitl_round_trip.py` covering #2430. Drive refine pipeline
          that registers HITL decision; observe AWAITING_HUMAN; call provide_input;
          assert pipeline resumes. Use `ScriptedProvider`.
        acceptance: |-
          Test passes on `main`. Reverting alive-signal bypass causes timeout.
        role: tester
        files:
          - integration_tests/regression/test_hitl_round_trip.py
      - id: TASK-3-6
        description: |-
          Add `test_brc_single_cycle.py` (BRC happy path: PROPOSE → ACK →
          CONFIRMED, exact counts) and `test_slice_dag_restart.py` (3-slice
          DAG, mid-flight restart_agent, assert slice-2 branch unchanged).
        acceptance: |-
          Both tests pass on `main`. BRC test asserts exact counts.
        role: tester
        files:
          - integration_tests/regression/test_brc_single_cycle.py
          - integration_tests/regression/test_slice_dag_restart.py
      - id: TASK-3-7
        description: |-
          Add `test_phase_aware_timeout.py`. Configure
          `phase_configs.plan.consensus_timeout_s = 30`; have planner not
          propose; assert `CONSENSUS_TIMEOUT` event lands within 30±5s;
          assert other phases unaffected.
        acceptance: |-
          Test passes on `main`. Setting timeout to 600 fails deadline assertion.
        role: tester
        files:
          - integration_tests/regression/test_phase_aware_timeout.py
      - id: TASK-3-8
        description: |-
          Add `test_babysit_pr_single_push.py`. Drive babysit-PR across 2
          coder revisions. Query gateway audit log for pushes to PR head ref;
          assert exactly 1 successful push.
        acceptance: |-
          Test passes on `main`. If regression makes coder push twice, test fails.
        role: tester
        files:
          - integration_tests/regression/test_babysit_pr_single_push.py
  - id: 4
    name: |-
      Wire integration tests into PR CI
    goal: |-
      Make integration tier run on every PR via `workflow_call` of
      `test-integration.yml` from `test.yml`. Per decision-3 default,
      check is NOT branch-protection-required from day 1.
    dependencies:
      - slice-3
    tasks:
      - id: TASK-4-1
        description: |-
          In `.github/workflows/test.yml`, add `integration:` job (sibling
          of `unit:` and `security:`) that uses `test-integration.yml`.
          Include in `aggregate:` job's `needs:` list. Add
          `timeout-minutes: 30`. Add `concurrency:` block to
          `test-integration.yml` mirroring `test.yml`.
        acceptance: |-
          PR shows new `Test / integration` check and updated
          `Test / aggregate` check that depends on it. Both run within
          30-min timeout. `make lint` passes.
        role: coder
        files:
          - .github/workflows/test.yml
          - .github/workflows/test-integration.yml
      - id: TASK-4-2
        description: |-
          In `.github/workflows/test-integration.yml`, add flake-guard steps:
          retry "Import images into k3s" once on failure; explicit
          `kubectl wait --for=condition=Available deployment/egg-orchestrator --timeout=120s`.
          Add per-step `timeout-minutes:`. On failure, capture k3s logs as artifact.
        acceptance: |-
          PR runs integration job to green. Image-import flake recovered by retry.
          On forced failure, uploads `k3s-debug.log` artifact.
        role: coder
        files:
          - .github/workflows/test-integration.yml
  - id: 5
    name: |-
      Documentation — point agents at the integration tier
    goal: |-
      Add Quick Reference bullet and dedicated subsection in `CLAUDE.md`.
      Document local k3s recipe in `docs/guides/testing.md` (k3s ONLY per
      operator direction; do NOT document kind/minikube as alternatives).
    dependencies:
      - slice-4
    tasks:
      - id: TASK-5-1
        description: |-
          In `CLAUDE.md`, add Quick Reference bullet:
          `make test-integration  # Cross-module regressions; requires k3s (see docs/guides/testing.md)`.
          Add new section after "Key Entry Points" titled "Integration tests"
          with paragraph pointing at `integration_tests/regression/` for
          cross-module bugs. Update Repo Layout row.
        acceptance: |-
          `CLAUDE.md` contains new bullet, new section, and updated Repo Layout row.
          `make lint` passes.
        role: documenter
        files:
          - CLAUDE.md
      - id: TASK-5-2
        description: |-
          In `docs/guides/testing.md`, add "Integration tests" section with
          three subsections:
          1. **What it covers** — k3s + mocked LLMs.
          2. **Running locally** — k3s-on-host recipe ONLY. Do NOT document
             kind or minikube as alternatives. Mention macOS users need a
             Linux VM. Mention required-check name `Test / aggregate`.
          3. **CI gating** — integration tier runs on every PR. `make test-all`
             remains unit-only.
        acceptance: |-
          New section with three subsections. NO mention of kind or minikube
          as alternative local-dev runtimes. `make lint` passes.
        role: documenter
        files:
          - docs/guides/testing.md
      - id: TASK-5-3
        description: |-
          Clean up stale references in adjacent docs:
          - `docs/architecture/kubernetes-migration.md`: mark
            `integration_tests/docker-compose.yml` row as historical with
            "(docker path retired in #2474)" annotation.
          - `docs/development/STRUCTURE.md`: remove `test_e2e_workflow.py`
            entry; remove or annotate `docker-compose.yml` entries.
          Do NOT delete historical sections — only annotate retired artifacts.
        acceptance: |-
          `grep -n "test_e2e_workflow" docs/development/STRUCTURE.md` returns no live references.
          `kubernetes-migration.md` mentions #2474 next to retired entries. `make lint` passes.
        role: documenter
        files:
          - docs/architecture/kubernetes-migration.md
          - docs/development/STRUCTURE.md
```