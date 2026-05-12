# `integration_tests/regression/`

k3s regression guards that pin invariants the SDLC pipeline has
regressed historically. Tests in here drive the real
`KubernetesSpawner` against the locally-deployed egg stack and
inspect resulting pod specs with `kubectl get pod -o yaml`. Per
[#2474](https://github.com/jwbron/egg/issues/2474), agents writing
these can't validate them locally — correctness is verified by the
`Test / aggregate` required check on the PR.

Originating issue: [#2632](https://github.com/jwbron/egg/issues/2632).

## What's covered today

| File | Invariant | Status |
|---|---|---|
| `test_slice_spawn_env_threading.py::test_each_slice_gets_its_own_branch_env` | Each per-slice spawn lands `EGG_BRANCH=egg/<pid>/slice-<N>` and `EGG_SLICE_ID=slice-<N>` on the pod spec even when an upstream `extra_env` ships a conflicting pipeline-level `EGG_BRANCH`. Sibling slices in the same pipeline get distinct Job names and distinct EGG_BRANCH refs. Pins #2428 + #2410 + #2403. | ✅ green |
| `test_slice_spawn_env_threading.py::test_baseline_spawn_without_extra_env_override` | Baseline: with no conflicting `extra_env`, the per-slice `branch` parameter still flows through to the pod's `EGG_BRANCH`. Catches a regression that would break the *default* env-derivation independent of the override path. | ✅ green |
| `test_long_name_round_trip.py` | A Job created with a name > 63 chars (triggering truncation in `create_container`) must be round-trippable through `delete_job` using the same input name. Direct regression guard for #2644. | ⚠️ `xfail(strict=True)` — blocked on [#2644](https://github.com/jwbron/egg/issues/2644) |
| `test_slice_restart_branch_invariants.py::test_restart_preserves_egg_branch_and_slice_id` | `restart_agent_job` for a slice-scoped agent preserves `EGG_BRANCH` and `EGG_SLICE_ID` on the new pod. The slice restart in #2632 starting-point #2. | ⚠️ `xfail(strict=True)` — blocked on [#2644](https://github.com/jwbron/egg/issues/2644) + [#2655](https://github.com/jwbron/egg/issues/2655) |
| `test_slice_restart_branch_invariants.py::test_restart_isolates_slice_from_pipeline_level_agent` | Restarting a pipeline-level agent of the same role doesn't disturb the slice-scoped Job's env or restart-budget. | ⚠️ `xfail(strict=True)` — blocked on [#2644](https://github.com/jwbron/egg/issues/2644) + [#2655](https://github.com/jwbron/egg/issues/2655) |

## Bugs surfaced while writing these tests

### #2644 — `KubernetesClient.delete_job` name-truncation asymmetry

`create_container` truncates Job names > 63 chars and appends an
8-char SHA digest. `delete_job` / `read_namespaced_job` /
`get_pod_for_job` do **not** apply the same truncation, so any
operation against the un-truncated name silently 404s when the Job
exists under the truncated form. The slice-DAG restart path
deterministically hits this for any pipeline-id / role
combination > 63 chars after prefixing (e.g.
`issue-2261-v9` + `slice-2` + `reviewer_agent_design`).

`test_long_name_round_trip.py` is the focused regression guard.

### #2655 — `restart_agent_job` races the Foreground deletion finalizer

Even after #2644 is fixed, `restart_agent_job`'s `Foreground`
deletion returns before the Job is removed from the API server —
the foreground finalizer waits for pods to terminate first. The
immediate respawn that follows then races the finalizer and 409s
on `AlreadyExists`. The two slice-restart tests need both #2644
and #2655 to land before they flip to passing.

## Gap audit — what should be in here but isn't yet

Pulled from [#2632](https://github.com/jwbron/egg/issues/2632) and
recent slice/BRC postmortems. Items below are seeds for future
expansion of this directory.

| Invariant | Why it matters | Why not yet |
|---|---|---|
| Slice DAG reaches `PR_READY` end-to-end with a mid-flight `restart_agent` | The full "starting point #2" of #2632. Pins that BRC consensus recovers cleanly across a slice-agent restart. | Requires real BRC consensus driving against a Claude provider, which a fresh CI runner cannot drive. See the ScriptedProvider pod-injection caveat in [#2474](https://github.com/jwbron/egg/issues/2474) and follow-up #2585. The branch-ref half is covered above. |
| Live-pod guard on `start_pipeline` recovery (#2420) | `start_pipeline` refuses without `force=true` when pods are live; pins #2420's regression. | Direct HTTP-level test against orchestrator routes. Reasonable to add in a follow-up; doesn't need agent pods to actually run. |
| Unpushed-commit salvage on push rejection (#2429) | A gateway push-rejection must produce an `egg/recovered/...` ref before the worktree is torn down. | Needs gateway push-failure injection — the existing `restricted-path` code path may be enough. Follow-up. |
| HITL round-trip (#2430) | Pipeline pauses on `AWAITING_HUMAN`, resumes on `provide_input`. Pins #2430. | Needs a pipeline that actually reaches HITL — requires either a scripted provider or a pre-seeded contract. Follow-up. |
| BRC single-cycle consensus message counts | Pins `feedback_brc_single_cycle.md`: producer PROPOSE → reviewer ACK → CONFIRMED in exactly N messages. | Needs ScriptedProvider pod injection. Deferred to #2585. |
| Phase-aware consensus timeouts honored end-to-end | Pins that `phase_configs.<phase>.consensus_timeout_s` is wired through to the actual timeout fired. | Implementable as a unit test against the orchestrator's timeout source-of-truth; the k3s tier doesn't add much. |
| Babysit-PR single final push to PR branch | Pins that 2 coder revisions produce exactly 1 push to the PR head ref. | Needs scripted provider for the coder revision loop. Deferred to #2585. |
| Slice teardown isolation under partial-failure | Deleting one slice's Job mid-flight doesn't disturb sibling slices' worktrees or sessions. | Achievable with the existing spawner harness once #2644 is fixed; adds another regression test to this directory. |
| `EGG_SLICE_ID` and `egg.slice.id` label parity | Today the slice scope is propagated as an env var on the pod but **not** as a Job/Pod label — operator queries like `kubectl get jobs -l egg.slice.id=slice-2` don't work, and the kubernetes_monitor can't filter by slice without parsing Job names. | Out of scope for #2632 (an operability gap, not a correctness regression) but worth opening separately if cluster-side slice introspection becomes important. |

## Conventions

- **Marker**: every file in here uses `pytestmark = pytest.mark.integration` so `make test-integration` picks them up.
- **Cleanup**: every test paired with a `cleanup_jobs` autouse fixture that calls `spawner.cleanup_pipeline(...)`. Without it, leftover Jobs in the test namespace persist across runs and force operators to `kubectl delete` by hand.
- **Role choice**: prefer roles in `_ROLES_WITHOUT_WORKTREE` (e.g. `REVIEWER_CODE`) for env-threading assertions — the spawner code paths in question are role-independent, and a worktree-free role keeps the test green on a fresh CI runner with `local_repos.paths: []`. Document the choice in the test's docstring when it matters.
- **xfail discipline**: `xfail(strict=True, reason=...)` only — never `skip` to hide a real failure. Strict ensures the test re-arms automatically when the blocking bug lands.
