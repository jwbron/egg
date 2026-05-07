# Analysis: Wire integration tests into PR CI; retire dead test tiers; expand coverage

> Issue: #2474 | Phase: refine

## Problem Statement

The repository ships a thoughtful integration-test suite at `integration_tests/`
(k3s-backed, with mocked LLMs, exercising gateway/container/network/policy
boundaries), but it is **not catching regressions** because:

1. **`test-integration.yml` is never invoked from a PR workflow.** The file
   only declares `workflow_call` and `workflow_dispatch` triggers, and no
   sibling workflow under `.github/workflows/` calls it. PRs merge to `main`
   without integration tests ever running. Compare with `test.yml`, which is
   wired into PRs directly via its own `pull_request:` trigger and is the only
   reason unit tests run on PRs today.
2. **Coverage gaps in the orchestrator pipeline state machine.** Recent
   in-process state-mutation regressions (#2428 slice spawn `EGG_BRANCH`
   threading, #2429 unpushed-commit salvage, #2420 live-pod guard on restart,
   #2430 HITL alive-signal bypass) all merged through CI green. They are
   plausibly catchable at the integration tier but no scenarios exercise the
   relevant code paths.

Two adjacent test tiers are dead code:

- **`tests/functional/`** — three test files (`test_git_wrappers.py:314`,
  `test_network_modes.py:254`, `test_session_lifecycle.py:345`) plus
  `conftest.py:487`, ~1,400 LoC total, all gated on the `functional` pytest
  marker. They start a docker-compose-based gateway. Last meaningful change
  2026-03-13 (#1053). No CI workflow references the marker; `make test-all`
  excludes them via the testpaths config; they are never run.
- **`test-e2e.yml`** — real-LLM (`ANTHROPIC_OAUTH_TOKEN`) weekly-cron workflow
  with two jobs (deterministic + agent-fuzz). Per the proposal text and
  #2449, the agent-flaky tier is too noisy to be useful as a regression
  signal.

## Recommended Approach

Option C: bundle A+B+C+D+F as the cleanup PRs, ship Part E as the expansion. All delivered within this single pipeline run via 5 sequential slices.

## Resolved Decisions (operator pre-refine answers)

- **decision-1** (PR shape): Multi-phase / multi-PR delivery within this single pipeline run. NOT one mega-PR, NOT deferred to a separate effort.
- **decision-2** (local-dev k3s runtime): **k3s only**. Do NOT document kind / minikube as alternatives in `docs/guides/testing.md`. Local k3s setup is a hard requirement for running integration tests.

## Open Questions (for implement-phase agents to resolve via defaults or HITL)

- **decision-3** (required-from-day-1): Whether the new integration check should be required for merge from day 1, non-blocking initially, path-conditional, or always-required.
- **decision-4** (e2e fate): Delete `test-e2e.yml` entirely, keep as placeholder, keep as manual-only, or defer to #2449.
- **decision-5** (ScriptedProvider location): Where `ScriptedProvider` should live and how it should be exposed.
- **decision-6** (push-rejection injection): Mechanism for injecting gateway push rejection in test E.3.
- **decision-7** (E.6 scenario): Precise scenario for test E.6 (Slice DAG with mid-flight `restart_agent`).
- **decision-8** (CLAUDE.md note placement): Where in `CLAUDE.md` the Part F note should live.
- **feedback-1 / Q1–Q6**: Wall-clock budget, flake guards, E.7/E.8 test shapes, test-tree placement, `make test-all` fold-in.

The plan picks pragmatic defaults for these; implement-phase agents can override if HITL answers arrive.

---

*Authored-by: egg (re-submitted at implement phase with qualifier v2 after pipeline state cleanup; original analysis at .egg-state/drafts/2474-analysis.md on origin/egg/issue-2474/work commit 0c92bab7d)*