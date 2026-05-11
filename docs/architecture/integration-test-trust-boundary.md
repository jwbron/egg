# Integration-Test Trust Boundary

Some test capabilities require **trusted-party** access — direct
orchestrator URLs, the `EGG_LIFECYCLE_SECRET`, pod-image swaps, the
ability to inject canned LLM trajectories into a running agent pod.
None of these are available to a sandboxed egg agent at runtime.

This page is the authoritative tier → fixture / route mapping. The
plan-phase reviewer's Trust-Boundary Audit
(`_get_plan_review_criteria` §10) references this page. The yardstick
is simple: **a task whose named primitives are not available in the
execution context the task assumes is a hard plan-phase NACK.**

## Why this matters

Plan-phase NACKs are cheap (the planner re-drafts a section).
Implement-phase NACKs on missing or wrong-tier primitives are
expensive: 8+ pod spawns per slice, ~60–90 min wall clock per
implement cycle. Issue [#2474](https://github.com/jwbron/egg/issues/2474)
burned ~10.7 h of compute before the operator aborted at decision-11,
because the plan named `ScriptedProvider` (a unit-test-only fake) as
if it could drive a deployed agent pod. The pattern is general
enough that we now require plan-phase reviewers to audit it
explicitly — see [#2594](https://github.com/jwbron/egg/issues/2594).

## Execution contexts

Every test, helper, or in-flight code path runs in exactly one of
these contexts. Plans should declare the context of any task that
touches the orchestrator, gateway, or k3s cluster.

### `in-sandbox-agent`

Driven by an egg agent pod (coder, tester, documenter, reviewer,
etc.). Code authored by an agent at SDLC time.

**Sees:**

- `gateway_url` (from `integration_tests/conftest.py`, the parent
  conftest).
- Gateway-mediated routes: `/api/v1/jira/*`, `/api/v1/confluence/*`,
  `/api/v1/git/*`, `/api/v1/gh/*`, anthropic proxy, etc.
- Outbound HTTP only via the gateway (private mode) when configured.

**Does NOT see:**

- `orchestrator_url`.
- Any `@require_lifecycle_secret`-gated route (`EGG_LIFECYCLE_SECRET`
  is not present in sandbox pods).
- Pod logs other than its own.
- The k8s API. Cannot list, exec into, or restart sibling pods.

**Cannot:**

- Inject a `ScriptedProvider` (or any custom LLM provider) into a
  deployed agent pod. Deployed pods run the real provider. There is
  no env var, ConfigMap, or entrypoint flag that swaps the LLM
  provider at pod runtime — and adding that infrastructure is its
  own scoped piece of work (track in a separate issue if needed).
- Drive another pipeline's HITL decisions, restart another agent,
  or cancel another task.

**Test files for this tier live under:** `integration_tests/`
(parent), and any sibling directory that doesn't add an
orchestrator-scoped fixture.

### `trusted-CI-runner`

Driven by pytest from outside the cluster — typically a CI runner,
or a developer's machine running `make test` against a local k3s
deployment. The runner is a trusted party: it holds
`EGG_LIFECYCLE_SECRET`, it can reach orchestrator URLs directly, and
it can read pod logs via `kubectl`.

**Sees:** everything `in-sandbox-agent` sees, plus:

- `orchestrator_url`
  (`integration_tests/local_pipeline/conftest.py:255`).
- `launcher_secret` (`integration_tests/local_pipeline/conftest.py:267`)
  — the value of `EGG_LIFECYCLE_SECRET` for the deployed
  orchestrator.
- All `@require_lifecycle_secret` routes. The decorator is wired
  in:

  - `orchestrator/routes/commit_authorship.py`
  - `orchestrator/routes/containers.py`
  - `orchestrator/routes/decisions.py`
  - `orchestrator/routes/deployment.py`
  - `orchestrator/routes/phases.py`
  - `orchestrator/routes/pipelines.py`

  (Use `grep -rn '@require_lifecycle_secret'
  orchestrator/routes/` to enumerate current usages.)
- `kubectl` access to pods in the deployed namespace.

**Test files for this tier live under:**
`integration_tests/local_pipeline/` (k3s-backed local pipeline
tests) and any sibling directory that explicitly requests the
trusted-runner fixtures.

### `human-operator`

Manual / `egg-orch` CLI invocations, HITL decision resolution, ad-hoc
operator commands. Not a test-execution context. Flag any task that
implicitly assumes an operator will step in mid-pipeline — that's
a workflow decision the plan should call out, not a test path.

## Fixture tier table

| Fixture | Defined in | Tiers that see it |
|---------|------------|-------------------|
| `gateway_url` | `integration_tests/conftest.py:71` (parent `EggStack`) and re-exported in `integration_tests/local_pipeline/conftest.py:261` | `in-sandbox-agent`, `trusted-CI-runner` |
| `egg_stack` | `integration_tests/conftest.py:277` | `in-sandbox-agent`, `trusted-CI-runner` |
| `gateway_session` | `integration_tests/conftest.py:294` | `in-sandbox-agent`, `trusted-CI-runner` |
| `isolated_container` | `integration_tests/conftest.py:428` | `in-sandbox-agent`, `trusted-CI-runner` |
| `external_container` | `integration_tests/conftest.py:441` | `in-sandbox-agent`, `trusted-CI-runner` |
| `test_container` | `integration_tests/conftest.py:454` | `in-sandbox-agent`, `trusted-CI-runner` |
| `local_pipeline_stack` | `integration_tests/local_pipeline/conftest.py:238` | `trusted-CI-runner` only |
| `orchestrator_url` | `integration_tests/local_pipeline/conftest.py:255` | `trusted-CI-runner` only |
| `launcher_secret` | `integration_tests/local_pipeline/conftest.py:267` | `trusted-CI-runner` only |

`pytest` resolves fixtures lexically from the nearest conftest
upward. A test file under `integration_tests/foo/` sees the parent
`integration_tests/conftest.py` but not the **sibling**
`integration_tests/local_pipeline/conftest.py`. Plans that put a
test that needs `orchestrator_url` outside `local_pipeline/` will
fail at collection time — NACK the plan.

## Scripted LLM trajectories

`ScriptedProvider` lives at
`shared/tests/test_egg_harness/test_integration.py:130`. It is a
test double for the harness module — unit-test scope only. A
`grep -rn ScriptedProvider sandbox/ k8s/ orchestrator/` returns
zero hits, and that is the design.

Tests that need canned LLM trajectories must pick **one** of:

1. **In-process unit tests** against the orchestrator's Python API,
   driving `ScriptedProvider` directly. This is the supported
   path today.
2. **Future: pod-level injection.** Swapping the LLM provider inside
   a deployed pod requires a ConfigMap / env-var / entrypoint
   contract that does not exist yet. Building that infrastructure
   is in scope for a separate issue (see #2585 for the related
   `TestCredentialIsolation` k3s rewrite); do not assume it in a
   plan unless that issue has landed.

A k3s integration test that "drives a refiner with
`ScriptedProvider`" is a hard plan-phase NACK until option (2)
exists.

## What plan-phase reviewers should do

For each task that interacts with the orchestrator, gateway, or
k3s cluster:

1. Identify the execution context (above).
2. Confirm every primitive the task names is available in that
   context (Primitive-Existence Audit, criteria §9).
3. Confirm the primitives are usable in that context, not just
   defined somewhere in the repo (Trust-Boundary Audit, criteria
   §10).

A mismatch — `orchestrator_url` used outside `local_pipeline/`,
`ScriptedProvider` referenced from `sandbox/`, a
`@require_lifecycle_secret` route called from an
`in-sandbox-agent` test — is a hard NACK. Name the specific
mismatch in your verdict so the planner can re-draft.

## Related

- [#2594](https://github.com/jwbron/egg/issues/2594) — plan-phase
  audit motivation, this doc, criteria §9 and §10.
- [#2474](https://github.com/jwbron/egg/issues/2474) — the
  10.7 h-compute incident that prompted the audit.
- [#2585](https://github.com/jwbron/egg/issues/2585) —
  `TestCredentialIsolation` k3s rewrite; touches the trusted-runner
  tier.
- [SDLC Pipeline architecture](sdlc-pipeline.md).
- [Orchestrator architecture](orchestrator.md).
