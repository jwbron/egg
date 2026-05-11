# Analysis: Wire integration tests into PR CI; expand coverage (Parts A, E, F remaining)

> Issue: #2474 | Phase: refine

## Problem Statement

PR #2556 (merged 2026-05-07) shipped Parts **B**, **C**, **D** of the original
six-part #2474 proposal — drop the `EGG_RUNTIME=docker` runtime branch from
`integration_tests/conftest.py`, delete the dead `tests/functional/` tier, and
retire the real-LLM `test-e2e.yml` weekly workflow. The repository now has a
clean k3s-only integration tier at `integration_tests/`, and the dead test
markers (`functional`, `e2e`, `agent_flaky`) are gone from `pyproject.toml`.

Three parts remain, and they are the substantive ones for catching
regressions:

1. **Part A — Make `test-integration.yml` a required PR check.** The
   workflow exists at `.github/workflows/test-integration.yml` with
   `workflow_call` and `workflow_dispatch` triggers, but **no PR workflow
   invokes it**. Grepping `.github/workflows/*.yml` for
   `uses: ./.github/workflows/test-integration.yml` returns no hits. PRs
   merge to `main` without integration tests ever running. This is exactly
   why the four regressions named in the original issue (#2428, #2429,
   #2420, #2430) all merged CI-green.
2. **Part E — Expand integration coverage.** Eight new k3s integration
   scenarios are specified in the issue body — four targeted at recent
   regressions and four at obvious-but-untested invariants. The Part E
   auxiliary task — promoting `ScriptedProvider` from
   `shared/tests/test_egg_harness/test_integration.py:130` to a public
   `shared/egg_harness/testing/scripted_provider.py` — is the prerequisite
   that lets the new tests hand each agent role a canned LLM trajectory.
3. **Part F — Tell agents about `integration_tests/`.** Currently the root
   `CLAUDE.md` does not mention `make test-integration` in Quick Reference,
   has no Integration-tests subsection, and `docs/guides/testing.md` has no
   Integration-tests section. Agents reading the project memory have no
   pointer toward this tier when they write tests.

The desired outcome is: every PR runs the integration tier; integration
coverage exists for the four named regression categories plus the four
state-machine invariants; agents reading `CLAUDE.md` discover the tier
naturally.

## Current Behavior

### CI wiring

| Workflow | Trigger | Status |
|---|---|---|
| `test.yml` | `pull_request`, `workflow_call`, `workflow_dispatch` | Required PR check (`unit`, `security`, aggregated by `aggregate`) |
| `test-integration.yml` | `workflow_call`, `workflow_dispatch` only | **Orphan** — no PR workflow calls it |
| `lint.yml` | `pull_request`, `workflow_call`, `workflow_dispatch` | Required PR check |
| `on-pull-request*.yml` | `pull_request` | Reviewer workflows (code, contract, agent-mode-design) |

`test-integration.yml` already does the right work (build images, `curl -sfL
https://get.k3s.io | sh -`, `scripts/install-calico.sh`, k3s image import,
`kubectl apply -k k8s/overlays/local/`, then
`pytest integration_tests -v -m "integration or security" --timeout=300`).
It just isn't called.

### `ScriptedProvider` location

`shared/tests/test_egg_harness/test_integration.py` defines
`ScriptedProvider` at line 130 and a module-private `_stream_events` helper
at line 39. Five tests in that file consume it (lines 203, 276, 360, 402,
473). `grep -rn "ScriptedProvider"` confirms no other consumers in the live
tree. Because the class lives under `shared/tests/`, anything outside
`shared/tests/test_egg_harness/` cannot import it.

### `integration_tests/` shape

The tier is organised into four subdirs plus loose top-level files:

```
integration_tests/
├── conftest.py            # EggStack, gateway/network helpers, k3s-only
├── sdlc/                  # full-pipeline scenarios (happy_path, hitl_flow, ...)
├── local_pipeline/        # API-level pipeline + worktree scenarios
├── test_babysit_pr/       # babysit-PR specific scenarios
└── test_*.py              # gateway / credentials / network / SDK loose tests
```

There is **no `integration_tests/regression/`** today. The conftest
docstring already calls out that `test_credential_security.py::
TestCredentialIsolation` is currently skipped on k3s (it used docker-only
fixtures that were retired in PR #2556) — a small known gap, but not the
focus of this issue.

### Regression code paths Part E targets

| Issue | Where it lives | Existing coverage |
|---|---|---|
| #2428 EGG_BRANCH per-slice threading | `orchestrator/kubernetes_spawner.py:755-757` | None at integration tier; no test exec's `kubectl get pod` on a slice coder to read its env |
| #2429 unpushed-commit salvage | `orchestrator/routes/pipelines.py` salvage path | None at integration tier; restricted-path gateway push rejection isn't exercised end-to-end |
| #2420 live-pod guard on restart | `orchestrator/routes/pipelines.py:992-1074` (`live_pods_present` / `force=true` branch) | None at integration tier; unit tests cover the helper but not the HTTP path |
| #2430 HITL alive-signal bypass | overseer code; orchestrator HITL flow | `integration_tests/sdlc/test_hitl_flow.py` exists but doesn't drive the full AWAITING_HUMAN → `provide_input` → RUNNING round-trip end-to-end |

### Phase-aware consensus timeouts (E.7 context)

The orchestrator's `resolve_consensus_timeout_minutes` at
`orchestrator/models.py:27` reads `consensus_timeout_minutes_<phase>` (note:
**minutes**, not seconds) from `PipelineConfig` and falls back to
`PHASE_CONSENSUS_TIMEOUT_DEFAULTS_MIN`. The issue text says
`phase_configs.plan.consensus_timeout_s = 30` (seconds), which doesn't
match the current field name. This is one of the open questions registered
below (feedback Q3).

### `CLAUDE.md` and `docs/guides/testing.md` current state

`CLAUDE.md` Quick Reference lists `make test`, `make test-all`, `make
lint`, `make lint-fix`, `make security`, `make setup`, `make help`, `make
deps` — no `make test-integration` bullet. The Repo Layout table mentions
`integration_tests/` ("Cross-component integration tests") but there's no
deeper note. `docs/guides/testing.md` covers the `make test` narrowing
model in depth (~150 lines) but never names the integration tier.

## Constraints

- **CI runner cost on every PR.** `test-integration.yml`'s end-to-end
  runtime today is ~5–10 min per the issue text. Adding Part E's 8 scenarios
  will lengthen it. Doubling PR wall-clock is observable; quadrupling it
  isn't acceptable.
- **PR-check flake budget.** Required checks that flake erode trust
  quickly. The issue recommends a settle-in window — observed across ~10
  PRs — before flipping required-for-merge. This is captured in decision-3
  below.
- **File-boundary discipline.** Refiner role can only push to
  `.egg-state/drafts/` and `.egg-state/agent-outputs/`. Implementation
  (workflow YAML, Python tests, doc files) will be coder/tester/documenter
  work in the implement phase.
- **`ScriptedProvider` is shared-package test code, not runtime code.**
  Promoting it under `shared/egg_harness/testing/` makes it part of the
  shipped package — a public API surface. Anything we export there we
  commit to maintaining. The issue explicitly chose
  `shared/egg_harness/testing/scripted_provider.py` so the question is
  settled, but the `testing/` submodule needs an `__init__.py` and the
  promotion needs to leave the existing five consumer call sites green.
- **Required-check name needs to be discoverable.** Branch-protection
  toggles in repo settings reference a specific check name. The issue
  doesn't pin one; the prior pipeline (`issue-2474-v2`) used
  `Test / aggregate` as the canonical name. If we wire integration into
  `test.yml`, that name stays correct. If we use a new top-level workflow,
  the name changes. Captured in decision-2 below.
- **Phase-timeout API granularity.** Implementing E.7 cleanly requires
  either a minutes-based assertion or a second-granular config field
  (the issue text uses `consensus_timeout_s` but the model field is
  `consensus_timeout_minutes_<phase>`). Captured in feedback Q3.
- **Test selector / changeset narrowing.** `integration_tests/` is NOT in
  the `PACKAGES` constant for `scripts/select_tests/`, so `make test`
  does not narrow integration tests. This is intentional — the
  `make test-integration` target is the canonical inner-loop call for
  this tier — but worth noting so the implement phase doesn't try to wire
  narrowing.
- **k3s-on-host is the only supported local runtime.** The operator's
  pre-refine direction (preserved from the prior pipeline's analysis at
  `.egg-state/drafts/issue-2474-v2-analysis.md`) says: document **k3s only**
  in `docs/guides/testing.md`; do NOT mention kind or minikube. macOS
  developers will need a Linux VM.

## Options Considered

### Option A: One PR (A + E + F together)

**Approach**: A single PR that promotes `ScriptedProvider`, adds the new
`integration_tests/regression/` subtree with 8 tests, wires
`test-integration.yml` into `test.yml` as an `integration` job, and adds
the `CLAUDE.md` + `docs/guides/testing.md` notes.

**Pros**:
- Atomic delivery — agents reading the merged repo see the gate, the
  expanded coverage, and the docs in lockstep.
- Single human review and approval pass.
- F's "where to put integration tests" note documents the regression
  subdir the same PR creates — no temporal gap where the docs reference
  a directory that doesn't exist yet.

**Cons**:
- Bigger diff (workflow YAML + 8 new tests + ScriptedProvider promotion +
  docs). Harder to bisect.
- If one test from Part E has a design question that holds review, it
  blocks the gate from landing.
- The gate goes live for the first time alongside 8 untested-in-CI
  scenarios; if any flakes, the whole PR's gate run is red.

### Option B: Two PRs — E first, then A + F

**Approach**: PR1 = `ScriptedProvider` promotion + all 8 new tests in
`integration_tests/regression/`. PR2 = wire `test-integration.yml` into
`test.yml` + the `CLAUDE.md` / docs notes.

**Pros**:
- PR1 lands the new tests against the existing
  `make test-integration` invocation path; they're proven green before
  the gate flips.
- PR2 is then small — workflow YAML + docs only — and easy to revert
  if the gate is flaky on day 1.
- Bisect granularity improves: workflow-wiring regression vs.
  test-content regression are separate commits.

**Cons**:
- Two coordinations, two reviews, two merges.
- PR1's tests run only manually (or via `workflow_dispatch`) until PR2
  ships; there's a window where the new tests are checked-in but
  unguarded.
- Part F's note in PR2 references the regression subdir created in PR1
  — a small temporal coupling but harmless.

### Option C: Three PRs — E, then A, then F

**Approach**: PR1 = `ScriptedProvider` + 8 new tests. PR2 = workflow
wiring. PR3 = docs.

**Pros**:
- Tightest bisect; smallest reviewable diffs.
- Docs PR is editor-only and trivially revertible.

**Cons**:
- Three coordinations.
- Docs lag two PRs behind the gate. Future agents reading mid-stream
  see the gate but not the pointer to the regression subdir.

### Option D: Two PRs — A + F first (advisory), E follow-up

**Approach**: PR1 = wire `test-integration.yml` into `test.yml` as a
non-blocking job + `CLAUDE.md` / docs notes. PR2 = `ScriptedProvider`
promotion + 8 new tests.

**Pros**:
- The gate ships first and immediately starts catching regressions in
  the existing scenarios (which already exercise gateway / network /
  SDLC paths).
- Settle-in starts day 1; flake observation begins before E adds new
  surface area.

**Cons**:
- Reverses the natural dependency: the gate runs against the *current*
  test set, which doesn't cover the four regressions Part E targets,
  so the new gate's first wins are limited to whatever the existing
  tier catches.
- PR1's docs in `CLAUDE.md` would mention `integration_tests/regression/`
  before the directory exists; either the doc text drifts to mention
  a future subdir, or it gets reworded later.

## Recommended Approach

**Option B** — ship E first, then A + F.

Rationale:

- **The gate's value is highest when the tests it gates exist.** Part E
  adds explicit coverage for the four regression categories that
  motivated this issue. Landing the gate against a tier that doesn't
  yet cover those regressions defers the actual win until PR2 lands
  anyway; landing E first means the very first run of the new gate
  exercises the new coverage.
- **Settle-in is smoother with proven tests.** The issue text recommends
  ~10 PRs of non-blocking observation before flipping required-for-merge.
  Starting that window with 8 untested-in-CI scenarios (Option A) burns
  the settle-in budget on early flakes that have nothing to do with
  the gate's value proposition. Running the new tests via
  `workflow_dispatch` on PR1's branch before PR2 lands gets us a couple
  of clean runs cheap.
- **Smaller, more reviewable diffs.** PR1 is "8 new tests + 1 module
  move". PR2 is "wire workflow + docs". Each fits a single review
  cycle.
- **Part F's docs reference the regression subdir.** That subdir exists
  by PR1; PR2's `CLAUDE.md` text can name it without temporal coupling.
- **Cost.** Two coordinations vs. three (Option C) is acceptable for
  the cleanness gain over Option A.

The recommendation is conditional on the operator's answer to
**decision-1**. If atomic delivery is preferred (Option A), the plan
phase will collapse PR1 + PR2 into one slice; if maximally split
(Option C) is preferred, F splits out into PR3. Option D is explicitly
NOT recommended because the gate's first wins should land with the
regression coverage that motivated this issue.

## Open Questions

### Resolved in Pre-Refine

- **Decision (prior pipeline)** — Multi-PR delivery is acceptable within
  this single pipeline run; do NOT collapse to a single mega-PR
  prematurely. (Source: prior pipeline `issue-2474-v2`, operator
  resolution on decision-1.) Re-asked here as
  **decision-1** below because the exact PR shape (1/2/3 PRs) for the
  remaining Parts A+E+F is still open.
- **Decision (prior pipeline)** — Document **k3s only** in
  `docs/guides/testing.md`; do NOT document kind / minikube as
  alternatives. Local k3s setup is a hard requirement for running
  integration tests. (Source: prior pipeline `issue-2474-v2`, operator
  resolution on decision-2.) Honored throughout Part F; not re-asked.
- **Issue body specifies** — `ScriptedProvider` lives at
  `shared/egg_harness/testing/scripted_provider.py`. Not re-asked.
- **Issue body specifies** — New tests live under
  `integration_tests/regression/` with a shared conftest. Not re-asked.
- **Issue body specifies** — Part F's `CLAUDE.md` text: a Quick Reference
  bullet (`make test-integration  # Cross-module regressions; requires
  k3s (see docs/guides/testing.md)`) plus a short subsection after "Key
  Entry Points". Not re-asked — only the open-vs-named-tests-in-the-note
  question is open (feedback Q6).
- **Issue body specifies** — Required-check policy direction:
  non-blocking with settle-in window across ~10 PRs before flipping
  required. Captured as the prior-pipeline default in decision-3
  options, which the operator can override.

### Multiple-choice decisions (registered via `egg-contract add-decision`)

#### decision-1: How should the remaining work (Parts A, E, F) be packaged into PRs?

- [ ] One PR containing A+E+F together (single review, atomic delivery)
- [ ] Two PRs: E (ScriptedProvider + new tests) first, then A+F together (wire gate once tests proven green)
- [ ] Three sequential PRs: E → A → F (smallest reviewable chunks, tightest bisect)
- [ ] Two PRs: A+F (wiring + docs) first as advisory, then E (expansion) follow-up
- [ ] Other (explain in reply)

#### decision-2: Where should the workflow_call into test-integration.yml live?

- [ ] Inside `.github/workflows/test.yml` as a new `integration` job sibling of `unit` and `security`, included in the existing aggregate
- [ ] Inside `.github/workflows/on-pull-request.yml` as a separate workflow_call alongside the existing code review
- [ ] A new top-level workflow file dedicated to integration tests with its own pull_request trigger
- [ ] Inline pull_request trigger added directly to `test-integration.yml` (no wrapper)
- [ ] Other (explain in reply)

#### decision-3: What gating policy should the new integration check use at merge time?

- [ ] Non-blocking from day 1; flip to required-for-merge after observed stability across ~10 PRs (matches issue text recommendation)
- [ ] Required-for-merge from day 1 (riskier; flake risk erodes trust quickly)
- [ ] Path-conditional required (e.g. only required when `integration_tests/`, `gateway/`, `orchestrator/`, or `k8s/` paths change)
- [ ] Required-for-merge from day 1 BUT with `continue-on-error: true` on flake-prone steps so transient failures don't fail the check
- [ ] Other (explain in reply)

#### decision-4: What is the precise scenario for test E.6 (Slice DAG with mid-flight restart_agent)?

- [ ] 3-slice DAG; restart slice-2's coder while it is in PROPOSE; assert slice-2's branch ref commit SHA is unchanged across the restart and the pipeline reaches PR_READY (prior-pipeline default for decision-7)
- [ ] 2-slice DAG; restart slice-1's coder during slice-2's REVIEW; assert no cross-slice consensus inheritance (matches #2535 invariant)
- [ ] 3-slice DAG; restart slice-2's coder mid-IMPLEMENT (before any PROPOSE); assert slice-2's worktree is recreated and a fresh PROPOSE eventually fires
- [ ] Generic restart-agent during any phase of any slice; assert the orchestrator never orphans the slice and reaches a terminal state
- [ ] Other (explain in reply)

#### decision-5: What is the acceptable wall-clock budget for the new Test / integration check on a PR, and what's the strategy if Part E's additions push it past that budget?

- [ ] 15 min p95 target; shard by pytest mark/dir if breached
- [ ] 20 min p95 target; keep single-job, add parallel pytest-xdist workers if breached
- [ ] Whatever the existing test-integration.yml runs at (~5-10 min today, ~10-15 with Part E adds); no shard plan needed yet
- [ ] No fixed budget; flag in a follow-up if PR latency becomes a complaint
- [ ] Other (explain in reply)

#### decision-6: Should `make test-all` (the local-dev full-suite target) eventually include the integration tier once a documented k3s-on-host recipe exists, or stay unit-only?

- [ ] Stay unit-only; `make test-integration` remains a separate explicit target (matches prior-pipeline default)
- [ ] Fold integration tier into `make test-all` once k3s recipe lands (single source of truth for "everything green")
- [ ] Add a new `make test-everything` target that runs both; leave `test-all` unit-only
- [ ] Decide later in a follow-up issue after observing local-dev demand
- [ ] Other (explain in reply)

#### decision-7: The existing `test_credential_security.py::TestCredentialIsolation` is currently skipped under k3s (per integration_tests/conftest.py docstring — it required docker-compose semantics that no longer exist). Should this gap be addressed in this issue, or out-of-scope?

- [ ] Out-of-scope: keep skip in place; track in a follow-up issue separate from #2474
- [ ] Add to Part E as a 9th k3s-native rewrite (delays Part E for credential-isolation rewrite)
- [ ] Delete the test entirely (treat the credential-isolation invariant as already covered by other tests)
- [ ] Convert to an xfail with a tracking issue link so it surfaces if it accidentally starts passing
- [ ] Other (explain in reply)

### Open-ended feedback (registered via `egg-contract add-feedback` as feedback-1)

- **Q1** — Flake guards: which sources of CI flake should we pre-emptively
  defend against in the workflow (image-import retry, kubectl wait
  timeouts, explicit cleanup on failure with kubectl-events artifact)?
  Any past CI flake you've observed that should be specifically pinned
  down?
- **Q2** — Test E.3 (unpushed-commit salvage, #2429): the issue text
  says "inject gateway push rejection (use existing restricted-path code
  path)". Concretely — should the test push a docs file from the coder
  role to force a real 403, or is there a different restricted-path code
  path you'd prefer?
- **Q3** — Test E.7 (phase-aware consensus timeouts): the issue says
  `phase_configs.plan.consensus_timeout_s = 30`, but
  `orchestrator/models.py:resolve_consensus_timeout_minutes` reads
  `consensus_timeout_minutes_<phase>` (minutes, not seconds). Should the
  test use minute-granular timing (e.g. 1-minute plan timeout, observe
  60±10s) or do we need to add second-granular config support first?
- **Q4** — Test E.8 (Babysit-PR single final push): the issue mentions
  `_babysit_final_push_head_move_guard` — should the test assert via the
  gateway audit log of push events, or via observing `git ls-remote` on
  the PR head ref before/after each coder revision?
- **Q5** — Once the new check is wired (non-blocking initially), what's
  the trigger for flipping required-for-merge? "10 PRs without flake" is
  mentioned in the issue — should this be a defined SLO (e.g. <1 flake
  per 20 runs measured over 2 weeks), or maintainer's judgment?
- **Q6** — Should Part F's `CLAUDE.md` text mention by name the specific
  tests added in Part E so future agents can locate them when they need
  similar coverage (e.g. "use
  `integration_tests/regression/test_slice_branch_env.py` as the template
  for slice-spawn env-threading regressions"), or keep the note generic
  to avoid stale references?

---

## Complexity Assessment

**Complexity: medium.**

- Multi-file change with clear scope: one workflow YAML edit (Part A),
  one module promotion + 8 new test files (Part E), two doc files
  (Part F).
- No architectural change. Each new test pins one regression or one
  invariant; the workflow change is a single `workflow_call`.
- Risk is concentrated in CI flake observation rather than code
  complexity. The gate's first run is the dogfood for itself.
- Could plausibly slice into 2–3 PRs (decision-1) but does not require
  cross-cutting redesign of any subsystem.

---

*Authored-by: egg*


## HITL Resolution

The following was approved by a human reviewer at the refine phase gate:

## Resolved Questions

**decision-1 (PR/slice packaging)**: Three slices = three PRs. slice-1 = Part E (ScriptedProvider promotion + 8 new tests under integration_tests/regression/). slice-2 = Part A (wire test-integration.yml into test.yml as new 'integration' job). slice-3 = Part F (CLAUDE.md + docs/guides/testing.md). Soft dep: slice-3 depends on slice-1 (F's docs reference the regression subdir E creates). slice-1 and slice-2 can run in parallel. NOTE: The refiner registered this as a 'PR count' decision; the operator clarified it should be framed in slice-DAG terms. Plan phase must produce a 3-slice DAG, not a single-slice plan with multiple PRs. Tracking issue for the refiner's framing confusion: #2584.

**decision-2 (where workflow_call lives)**: Inside .github/workflows/test.yml as a new 'integration' job sibling to 'unit' and 'security', included in the existing 'aggregate'. This keeps 'Test / aggregate' as the canonical required-check name (slice-2 implementation detail).

**decision-3 (gating policy)**: Required-for-merge from day 1. NOT the issue text's recommended non-blocking-with-settle-in approach. Operator chose tighter gating; flake risk is accepted.

**decision-4 (E.6 scenario)**: 3-slice DAG; restart slice-2's coder while it is in PROPOSE; assert slice-2's branch ref commit SHA is unchanged across the restart and the pipeline reaches PR_READY.

**decision-5 (wall-clock budget)**: No fixed budget. Existing test-integration.yml runtime (~10-15 min after Part E adds) is acceptable. Flag in a follow-up if PR latency becomes a complaint; no shard plan needed yet.

**decision-6 (test-all inclusion)**: Stay unit-only. `make test-integration` remains a separate explicit target. Do not fold integration tier into `make test-all`.

**decision-7 (TestCredentialIsolation gap)**: Out-of-scope for #2474. Tracked in follow-up issue #2585.

**Q1 (flake guards)**: Defend against image-import flakes (retry), kubectl wait timeouts (explicit deadlines), and on-failure capture (`kubectl get events --all-namespaces` + pod logs as workflow artifact). Standard k3s harness hardening set.

**Q2 (E.3 push-rejection mechanism)**: Coder role pushes a docs file to force a real 403 from the gateway's restricted-path policy. Exercises the real code path; no mocks.

**Q3 (E.7 timeout granularity)**: Use minute-granular timing. Configure `consensus_timeout_minutes_plan = 1`; observe CONSENSUS_TIMEOUT event fires within 60±10s. Do NOT add second-granular config support in this pipeline — the existing minutes-based API is sufficient for the assertion. Update the test's docstring to note the issue text's `consensus_timeout_s = 30` was a typo (the real field is `consensus_timeout_minutes_<phase>`).

**Q4 (E.8 push-counting method)**: Gateway audit log of push events. Authoritative source; counts every push attempt and outcome.

**Q5 (required-flip SLO)**: N/A — operator chose Required-from-day-1 for decision-3, so the non-blocking → required transition does not exist.

**Q6 (CLAUDE.md naming)**: Generic note pointing at integration_tests/regression/. No specific test files named — keeps the docs low-maintenance.

## Scope Expansion (operator pre-refine instruction)

When a new regression test fails against current `main`, the pipeline MUST diagnose the production-code root cause and fix it in the relevant slice's PR — these tests are designed to catch real regressions; closing those gaps is in scope. When a new test is itself broken or flaky, fix the test rather than skipping or marking xfail.

## Plan-Phase Instructions

- Produce a 3-slice DAG: slice-1 (E), slice-2 (A), slice-3 (F). slice-3 has dep on slice-1.
- slice-1 owns: ScriptedProvider promotion to shared/egg_harness/testing/scripted_provider.py (+ shared/egg_harness/testing/__init__.py); 8 new tests in integration_tests/regression/ with shared conftest; minutes-based assertion in E.7; gateway-audit-log assertion in E.8; coder-pushes-docs-file mechanism for E.3; 3-slice/restart-slice-2-in-PROPOSE scenario for E.6.
- slice-2 owns: workflow_call invocation in test.yml under a new 'integration' job, included in 'aggregate'; flake guards (image-import retry, kubectl wait timeouts, on-failure kubectl-events artifact); PR description must document the exact required-check name to flip in repo Settings → Branch protection.
- slice-3 owns: Quick-Reference bullet in CLAUDE.md (`make test-integration  # Cross-module regressions; requires k3s (see docs/guides/testing.md)`); short Integration-tests subsection in CLAUDE.md after 'Key Entry Points' (generic, no named test files); top-level Integration-tests section in docs/guides/testing.md (k3s-only setup recipe, required-check name, CI gating notes).
- Each slice's failing tests against `main` (if any) trigger production-code fixes in that slice's PR per the scope expansion above.
