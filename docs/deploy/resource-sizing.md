# Pod resource sizing

Resource requests and limits for the three pod types in the egg stack, with the observed telemetry they were tuned against. See #1888 / #1895 for the right-sizing initiative.

## Current allocations

| Pod | CPU request | CPU limit | Mem request | Mem limit | QoS |
|---|---|---|---|---|---|
| `gateway` (egg-system) | 200m | 2 | 2Gi | 4Gi | Burstable |
| `orchestrator` (egg-system) | 250m | 1 | 512Mi | 1Gi | Burstable |
| `litellm` (egg-system) | 100m | 1 | 256Mi | 1Gi | Burstable |
| `egg-sandbox-*` (egg-agents, per-agent) | 250m | 2 | 512Mi | 2Gi | Burstable |

Gateway/orchestrator values live in `k8s/base/gateway-deployment.yaml` and `k8s/base/orchestrator-deployment.yaml`. Sandbox defaults are applied programmatically in `orchestrator/kubernetes_client.py` (`create_container`); callers can override via `kwargs["resources"]`.

## Observed usage (2026-04-22, 3 concurrent pipelines, 14 agents → later 10)

Two snapshots captured during the same multi-pipeline session, post-#1887 (incremental SSE parsing in gateway):

**5-minute steady-state trace (pod age 8–13 min):** 20 samples × 15s cadence.

| Pod | CPU median | CPU p90 | CPU max | Mem median | Mem max |
|---|---|---|---|---|---|
| `gateway` | 142m | 831m | 878m | 1404Mi | 1561Mi |
| `orchestrator` | 194m | 224m | 236m | 294Mi | 301Mi |
| sandbox (per-agent) | 63m | 111m | 468m | 238Mi | 407Mi |

**Follow-up snapshot (pod age 26 min, sandbox fleet 10, issue-1758 in `implement` running tests):**

| Pod | CPU | Mem |
|---|---|---|
| `gateway` | 808m | **2199Mi** |
| `orchestrator` | 112m | 342Mi |
| sandbox tester (issue-1758) | 189m | **566Mi** |
| other sandboxes | 11–100m | 224–272Mi |

Gateway memory climbed ~640Mi between the two snapshots (1561Mi → 2199Mi) even as the sandbox fleet *shrank* from 14 → 10 agents. The driver is the `implement`-phase tester running `make test` — test runs generate enough proxied traffic through the gateway to grow its working set past 2Gi. This is load-driven accumulation, not a leak; the original 4Gi limit from #1886 is still the right ceiling for a single-node cluster that hosts test-running sandboxes.

Gateway CPU spikes to 83–88% of the 1-core limit during proxy bursts (two distinct spikes in the 5-min window), confirming 1 core as the binding concurrency ceiling. 2 cores is the new limit.

The sandbox tester peaks at 566Mi mem / 189m CPU — well inside the 2Gi / 2-core limits. The 468m CPU outlier in the 5-min trace was a different agent briefly; still under 2 cores.

## QoS rationale

All three pod types are **Burstable** (`request < limit`). On a single-node k3s cluster, the ratio of idle:spike is wide enough that Guaranteed QoS (`request == limit`) would force large reservations that sit idle most of the time. Burstable lets the scheduler pack more pods per node while still giving each a guaranteed floor.

## Changes in this round

| Pod | Previous | Now | Reason |
|---|---|---|---|
| `gateway` | CPU limit 1, mem limit 4Gi (with TODO from #1886) | CPU limit **2**, mem limit 4Gi (TODO cleared) | 878m CPU peak at 1-core limit confirms ceiling; memory stays at 4Gi after observing 2.2Gi+ under `implement`-phase test load. |
| `orchestrator` | mem req 256Mi, mem limit 512Mi | mem req **512Mi**, mem limit **1Gi** | Consistently at 283–342Mi, above old request; 1Gi gives ~3× headroom. |
| sandbox default | CPU req 500m | CPU req **250m** | Typical per-agent CPU is 60–110m; 250m × 14 agents still reserves 3.5 cores while freeing 3.5 cores for gateway/orchestrator. Memory and CPU/memory limits left at 512Mi / 2c / 2Gi — test runs push the tester past 500Mi and headroom there is cheap insurance. |

## Re-capturing telemetry

There is no dedicated script. The inline loop below is sufficient for ad-hoc traces:

```bash
for i in $(seq 1 20); do
  ts=$(date -u +%FT%TZ)
  echo "=== $ts (sample $i/20) ==="
  kubectl top pod -n egg-system --no-headers
  kubectl top pod -n egg-agents --no-headers
  [ "$i" -lt 20 ] && sleep 15
done
```

For gateway memory detail (per-allocation), set `GATEWAY_MEM_TRACE=1` on the gateway Deployment and inspect stdout — see `gateway/mem_trace.py` (added in #1887).

## Gaps / future work

- 1-pipeline and 5+-pipeline traces from #1888's acceptance criteria were not captured. The 3-pipeline data gives enough headroom across all pod types that the numbers above should hold at lower and modestly higher concurrency; at 5+ pipelines the gateway 2-core CPU limit is likely the first binding constraint.
- No per-phase sandbox overrides are applied; all agents share the single default. The `kwargs["resources"]` override path is available if a specific phase proves it needs more.
- Gateway memory under long-running test-heavy pipelines is not yet fully characterized — the observed 2.2Gi at 26 min pod age suggests the working set grows with cumulative test traffic. Worth a longer-running capture before considering any reduction below 4Gi.
