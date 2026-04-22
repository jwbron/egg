# Pod resource sizing

Resource requests and limits for the three pod types in the egg stack, with the observed telemetry they were tuned against. See #1888 / #1895 for the right-sizing initiative.

## Current allocations

| Pod | CPU request | CPU limit | Mem request | Mem limit | QoS |
|---|---|---|---|---|---|
| `gateway` (egg-system) | 200m | 2 | 1Gi | 2Gi | Burstable |
| `orchestrator` (egg-system) | 250m | 1 | 512Mi | 1Gi | Burstable |
| `egg-sandbox-*` (egg-agents, per-agent) | 250m | 1 | 384Mi | 1Gi | Burstable |

Gateway/orchestrator values live in `k8s/base/gateway-deployment.yaml` and `k8s/base/orchestrator-deployment.yaml`. Sandbox defaults are applied programmatically in `orchestrator/kubernetes_client.py` (`create_container`); callers can override via `kwargs["resources"]`.

## Observed usage (2026-04-22, 3 concurrent pipelines)

5-minute trace, 15s sampling cadence, 20 samples. Three pipelines (two in `refine`, one in `implement`) produced 14 concurrent sandbox agents. Captured post-#1887 (incremental SSE parsing in gateway).

| Pod | CPU median | CPU p90 | CPU max | Mem median | Mem max | % of new limit (peak) |
|---|---|---|---|---|---|---|
| `gateway` | 142m | 831m | 878m | 1404Mi | 1561Mi | CPU 44%, Mem 76% |
| `orchestrator` | 194m | 224m | 236m | 294Mi | 301Mi | CPU 24%, Mem 29% |
| sandbox (per-agent) | 63m | 111m | 468m | 238Mi | 407Mi | CPU 47%, Mem 40% |

Gateway CPU spikes twice in the window (t=150s and t=225s, both hitting ~85% of the previous 1-core limit), confirming that the 1-core cap was the binding constraint on concurrent pipeline throughput — not memory. Gateway memory sits steady at 1.37–1.56Gi post-#1887, well below the interim 4Gi cap from #1886.

Sandbox agents: most sit at 60–110m CPU / 220–270Mi memory. Occasional per-agent spike to 468m CPU / 407Mi mem is within headroom of the 1-core / 1Gi limits. Fleet-aggregate peak was 1322m CPU / 3706Mi memory across 14 agents.

## QoS rationale

All three pod types are **Burstable** (`request < limit`). On a single-node k3s cluster, the ratio of idle:spike is wide enough that Guaranteed QoS (`request == limit`) would force large reservations that sit idle most of the time. Burstable lets the scheduler pack more pods per node while still giving each a guaranteed floor.

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

For gateway memory detail (per-allocation), set `EGG_GATEWAY_MEM_TRACE=1` on the gateway Deployment and inspect stdout — see `gateway/mem_trace.py` (added in #1887).

## Gaps / future work

- 1-pipeline and 5+-pipeline traces from #1888's acceptance criteria were not captured. The 3-pipeline data shows enough headroom across all pod types that the numbers above are expected to hold at lower concurrency; at higher concurrency the gateway 2-core CPU limit is likely to be the first binding constraint.
- No per-phase sandbox overrides are applied; all agents share the single default. The `kwargs["resources"]` override path is available if a specific phase (e.g. `implement` with heavy `make test` runs) later proves it needs more.
