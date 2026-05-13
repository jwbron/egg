# MCP Deployment Tools Reference

The MCP server exposes Kubernetes-facing tools for agents and operators to
introspect the cluster, validate committed deployment manifests, prune stale
worktrees, confirm NetworkPolicy enforcement, read service logs, and kick
off a rebuild+rollout without dropping to raw `kubectl` /
`k3s ctr images import`. The first five landed in
[#1759](https://github.com/jwbron/egg/issues/1759) to close the diagnostic
blind spot surfaced during the Docker → k3s migration validation
([#1553](https://github.com/jwbron/egg/issues/1553),
[#1692](https://github.com/jwbron/egg/issues/1692));
[`get_service_logs`](#get_service_logs) followed in
[#1853](https://github.com/jwbron/egg/issues/1853) to cover gateway / orchestrator
pod logs that `get_container_logs` (agent-sandbox only) does not reach.

| Tool | Mutates cluster? | Runtime | Auth |
|------|------------------|---------|------|
| [`get_deployment_context`](#get_deployment_context) | No | multi-runtime (k8s, Docker) | `@require_lifecycle_secret` |
| [`validate_deployment_manifests`](#validate_deployment_manifests) | No | k8s only (Docker returns `not_available_on_runtime`) | `@require_lifecycle_secret` |
| [`prune_stale_worktrees`](#prune_stale_worktrees) | Yes (host filesystem) | k8s only | `@require_lifecycle_secret` on orchestrator proxy; gateway session token on the gateway route |
| [`validate_network_isolation`](#validate_network_isolation) | Yes (spawns short-lived probe Job) | k8s only | `@require_lifecycle_secret` |
| [`rebuild_and_rollout`](#rebuild_and_rollout) | Yes (rebuilds images + restarts Deployments) | k8s only | `@require_lifecycle_secret` |
| [`get_service_logs`](#get_service_logs) | No | k8s only | `@require_lifecycle_secret` |

All six tools live in `PIPELINE_TOOLS` (`orchestrator/mcp_tools.py`) with
handlers on `PipelineToolHandler`. The server is rate-limited to 30 req/min
— the diagnostic skills that compose these tools cap themselves at
≤10 primitive calls per invocation. See
[`/deployment-diagnose`](../../skills/deployment-diagnose/SKILL.md) and
[`/agent-diagnose`](../../skills/agent-diagnose/SKILL.md).

## Runtime Gating

All six deployment tools branch on the `EGG_RUNTIME` env var, which is
read at the orchestrator process boundary. When `EGG_RUNTIME` is unset,
the orchestrator auto-detects: if `KUBERNETES_SERVICE_HOST` is present
(injected into every pod by the apiserver) the runtime is inferred to be
`"kubernetes"`; otherwise it defaults to `"docker"`. The resolved runtime
and its provenance are returned on `get_deployment_context` as
`runtime` + `detection_source` (values: `"env"`, `"auto:k8s-service-host"`,
`"auto:default"`). Issue
[#1850](https://github.com/jwbron/egg/issues/1850) tracked the earlier
silent-docker-default that hid in-cluster misconfigs.

When `EGG_RUNTIME != "kubernetes"`, the five k8s-only tools return:

```json
{"error": "not_available_on_runtime", "runtime": "docker"}
```

rather than failing the MCP call. `get_deployment_context` is portable — it
returns a Docker-analog payload on the Docker runtime (matching the
pre-k3s deployment model).

When the orchestrator claims `"kubernetes"` but every cluster introspection
probe fails (apiserver unreachable, RBAC denial, missing kubeconfig),
`get_deployment_context` demotes `runtime` to `"unknown"` and attaches a
`detection_error` field (for example `"cluster_unreachable"`,
`"kubernetes_client_init_failed"`). `rebuild_and_rollout` refuses with a
distinct payload so operators can tell "apiserver unreachable" apart from
"you're on docker":

```json
{"error": "runtime_detection_failed", "runtime": "unknown", "detail": "..."}
```

`validate_network_isolation` additionally gates on
`get_deployment_context.network_policy_enforcement`. If the detected CNI
does not enforce NetworkPolicies (for example, vanilla Flannel — see
[network isolation](../architecture/network-isolation.md)), the tool
short-circuits with:

```json
{"error": "network_policy_enforcement_not_detected", "cni": "flannel"}
```

rather than returning misleading probe results (DEP-3 mitigation in the
#1759 risk register).

## Auth

Every orchestrator route added in #1759 is gated by
`@require_lifecycle_secret`, matching the
[#1769](https://github.com/jwbron/egg/issues/1769) fix pattern. An
unauthenticated request returns HTTP 401. Integration tests in
`integration_tests/test_k8s_deployment_tools.py` assert 401 on each new
route as a regression guard. The gateway route behind
`prune_stale_worktrees` (`POST /api/v1/worktrees/prune`) uses the existing
gateway session-token pattern instead — the orchestrator proxy handles the
lifecycle-secret check, then forwards an authenticated gateway call.

## `get_deployment_context`

Read-only cluster introspection.

**HTTP route**: `GET /api/v1/deployment/context`

**Input schema**: no arguments.

**Output shape** (Kubernetes runtime):

```json
{
  "runtime": "kubernetes",
  "detection_source": "env",
  "kubeconfig_context": "default",
  "cluster_info": {"server_version": "v1.30.2+k3s1"},
  "namespace": "egg-system",
  "cni": "calico",
  "network_policy_enforcement": true,
  "images": {
    "orchestrator": "egg-orchestrator:sha-abc1234",
    "gateway": "egg-gateway:sha-abc1234",
    "agents": "egg-sandbox:sha-abc1234"
  }
}
```

When cluster introspection succeeds but listing deployments comes back
empty (RBAC denial, empty namespace), `images_unavailable: true` is added
so operators know the empty map reflects a partial failure rather than a
cluster with no egg workloads. Similarly, `cluster_info.nodes_unavailable: true`
is set when the version probe succeeds but the node-list probe fails,
distinguishing "zero nodes" from "count unknown".

**Output shape** (Docker runtime — degrade-gracefully mode):

```json
{
  "runtime": "docker",
  "docker_version": "24.0.6",
  "compose_project": "egg",
  "namespace": null,
  "cni": null,
  "network_policy_enforcement": false,
  "images": {
    "orchestrator": "egg-orchestrator:local",
    "gateway": "egg-gateway:local",
    "agents": "egg-sandbox:local"
  }
}
```

**k3s detection heuristic** (applied in order, first match wins):

1. `kubectl get nodes -o json` shows any node whose `nodeInfo.kubeletVersion`
   ends in `+k3s<N>` (for example, `v1.30.2+k3s1`).
2. The `kube-system` namespace contains a DaemonSet whose container image
   name contains `rancher/k3s` or `rancher/mirrored-k3s-*`.

**Example MCP call**:

```python
result = await mcp.call_tool("get_deployment_context", {})
assert result["runtime"] == "kubernetes"
assert result["images"]["orchestrator"].startswith("egg-orchestrator:")
```

Every diagnostic skill calls this first — it bounds every subsequent check.

## `validate_deployment_manifests`

Static kustomize-overlay checks against five warn-on rules that caught the
concrete bugs in the #1692 validation pass.

**HTTP route**: `POST /api/v1/deployment/validate-manifests`

**Input schema**:

```json
{"overlay_path": "k8s/overlays/local"}
```

`overlay_path` is optional — omitting it selects the active overlay
resolved from the deployment context.

**Output shape**:

```json
{
  "warnings": [
    {
      "rule": "missing-secret",
      "severity": "error",
      "resource": "Deployment/egg-orchestrator",
      "message": "references Secret 'egg-lifecycle-secret' which is not present in the overlay"
    },
    {
      "rule": "selector-label-mismatch",
      "severity": "warn",
      "resource": "Service/egg-gateway",
      "message": "selector 'app=egg-gateway' does not match any Pod labels in the overlay"
    }
  ]
}
```

**Rules**:

1. `missing-secret` — a Deployment references a Secret that does not exist
   in the overlay.
2. `missing-hostpath` — a Pod specifies a `hostPath` volume that does not
   exist on the host.
3. `missing-image` — a container image tag does not match any image
   actually built by `make build`.
4. `selector-label-mismatch` — a Service selector matches zero Pod labels.
5. `env-var-collision` — two ConfigMaps / Secrets map the same env-var name
   into the same container with different values.

k3s-specific warnings (for example, `containerd image presence`) are
**gated on the k3s-detection heuristic** from
[`get_deployment_context`](#get_deployment_context). On non-k3s Kubernetes
flavors those rules are skipped with a structured entry:

```json
{"skipped": "not_k3s", "detected_runtime": "eks"}
```

so callers see *why* the rule did not fire.

**Non-k8s runtime**:

```json
{"error": "not_available_on_runtime", "runtime": "docker"}
```

**Example MCP call**:

```python
result = await mcp.call_tool("validate_deployment_manifests", {})
assert len(result["warnings"]) == 0  # clean overlay
```

The integration test `test_k8s_deployment_tools.py` asserts this tool
returns zero warnings on the committed overlay as a regression guard.

## `prune_stale_worktrees`

Clean up stale git worktrees plus orphaned directories under
`~/.egg-worktrees/`. Runs on both the `git worktree` administrative state
(`git worktree prune`) and the filesystem directory listing
(`WorktreeManager.cleanup_orphaned_worktrees`).

**Input schema**:

```json
{"dry_run": true}
```

`dry_run` is optional and defaults to `true`. The tool always sweeps every
configured repo — there is no per-repo scope parameter.

**Output shape**:

```json
{
  "git_worktree_prune": {
    "owner/repo": ["/home/egg/.egg-worktrees/stale-1", "/home/egg/.egg-worktrees/stale-2"]
  },
  "orphan_dirs": ["/home/egg/.egg-worktrees/orphan-dir-3"],
  "dry_run": true
}
```

When `dry_run=true` (the default), the tool reports what **would be**
removed but does not mutate the filesystem. When `dry_run=false`, the
reported paths are removed.

**HTTP routes**:

- Orchestrator proxy (required for MCP clients):
  `POST /api/v1/deployment/prune-worktrees` —
  gated by `@require_lifecycle_secret`.
- Gateway route (hosts the worktree mutex and actually does the work):
  `POST /api/v1/worktrees/prune` — gated by the gateway session-token
  pattern.

The orchestrator proxy calls the gateway route via `GatewayClient` because
the gateway holds the in-process worktree mutex. Every candidate path is
validated with `Path.resolve()` + `is_relative_to(WORKTREE_BASE_DIR)`
before deletion (RISK-2 path-traversal guard).

**Non-k8s runtime**:

```json
{"error": "not_available_on_runtime", "runtime": "docker"}
```

**Example MCP call**:

```python
# Dry run first — always
dry = await mcp.call_tool("prune_stale_worktrees", {"dry_run": True})
# Review dry["git_worktree_prune"] and dry["orphan_dirs"] before mutating.

# Apply once confirmed
wet = await mcp.call_tool("prune_stale_worktrees", {"dry_run": False})
```

## `validate_network_isolation`

Confirm that the NetworkPolicies in `k8s/base/network-policies.yaml` are
actually enforced by the CNI. Spawns a throwaway probe Job in `egg-agents`
that performs four probes and returns a structured allow/deny matrix.

**HTTP route**: `POST /api/v1/deployment/validate-network-isolation`

**Input schema**:

```json
{"pipeline_id": "issue-1759-v3", "role": "coder"}
```

`pipeline_id` is required. `role` defaults to `"coder"` if omitted.

**Output shape**:

```json
{
  "gateway_reachable": true,
  "internet_blocked": true,
  "agent_pods_unreachable": true,
  "orchestrator_api_reachable": true,
  "probe_job": "egg-probe-<uuid>",
  "probe_pod_phase": "Succeeded"
}
```

All four boolean fields should be `true` for a correctly isolated agent
(the agent can reach the gateway for proxied API calls and heartbeat the
orchestrator on `:9849`; nothing else). Any `false` indicates a
NetworkPolicy regression. `orchestrator_api_reachable` was previously
named `orchestrator_direct_blocked` with inverted polarity, which read
backwards from intent — `allow-agent-to-orchestrator` deliberately
permits the heartbeat path (#2652).

**Probe Job design** (RISK-1 mitigation):

- Labels: `app.kubernetes.io/component=agent`, `egg.probe=true`,
  `egg.io/probe-id=<uuid>` — matches what the agent-targeting
  NetworkPolicies gate on.
- Env: constructed via the `_PROTECTED_ENV_KEYS` denylist (no lifecycle
  secret, no session token, no gateway bearer).
- `automountServiceAccountToken=false` — the probe cannot pivot via the
  default service account.
- `ttlSecondsAfterFinished=0` — Kubernetes deletes the Job immediately
  after completion.
- `activeDeadlineSeconds=30` — the probe cannot hang.

**Pre-flight gate**: before spawning, the tool calls
[`get_deployment_context`](#get_deployment_context) and verifies
`network_policy_enforcement` is truthy. If the CNI does not enforce
NetworkPolicies, the tool returns:

```json
{"error": "network_policy_enforcement_not_detected", "cni": "flannel"}
```

rather than returning misleading probe results.

**Non-k8s runtime**:

```json
{"error": "not_available_on_runtime", "runtime": "docker"}
```

**Example MCP call**:

```python
result = await mcp.call_tool("validate_network_isolation", {
    "pipeline_id": "issue-1759-v3",
    "role": "coder",
})
assert result["gateway_reachable"] is True
assert result["internet_blocked"] is True
assert result["agent_pods_unreachable"] is True
assert result["orchestrator_api_reachable"] is True
```

The expected allow/deny matrix is documented in
[network isolation](../architecture/network-isolation.md) and asserted
end-to-end in `integration_tests/test_k8s_deployment_tools.py`.

## `rebuild_and_rollout`

Thin MCP wrapper over `make redeploy` (the committed Makefile target that
chains `docker build` → `k3s ctr images import` → `kubectl rollout
restart`). Exists because `make redeploy` routinely takes more than
60 seconds — well past FastMCP's tool-call budget — so the MCP tool
**returns immediately with a progress-stream handle** rather than blocking
on the shell.

**HTTP route**: `POST /api/v1/deployment/rebuild-and-rollout`

**Input schema**:

```json
{"wait": false}
```

`wait` is optional and defaults to `false`. When `wait=true`, the MCP
handler long-polls the progress stream via the existing consumer helper
and returns the terminal record to the caller in one round-trip.

**Output shape** (default, `wait=false`):

```json
{"progress_stream_id": "progress-abc123"}
```

Consume the stream via `orchestrator/routes/progress.py` (reuses the
existing progress-stream machinery used elsewhere in the orchestrator).
Lines stream in as interleaved stdout/stderr records. The stream closes
with a terminal record:

```json
{
  "phase": "done",
  "exit_code": 0,
  "rolled_out_images": {
    "egg-orchestrator": "sha-abc1234",
    "egg-gateway": "sha-abc1234",
    "egg-sandbox": "sha-abc1234"
  }
}
```

**Output shape** (`wait=true`):

The terminal record above, returned synchronously after the stream closes.

**Idempotency guard** (HITL decision-8 resolution, plan Q2): a second call
while a rollout is in flight returns HTTP 409 with the first call's
stream id:

```json
{
  "error": "rollout_already_in_progress",
  "progress_stream_id": "progress-abc123"
}
```

rather than spawning a second `make redeploy` that would race the first.
The guard is an in-process `asyncio.Lock` / boolean flag
(`_REBUILD_IN_PROGRESS`); the flag clears after the inner `make redeploy`
terminates (zero or non-zero exit), so a subsequent retry can proceed.

**Non-k8s runtime**:

```json
{"error": "not_available_on_runtime", "runtime": "docker"}
```

No subprocess is invoked in this branch.

**Example MCP call** (fire-and-forget):

```python
result = await mcp.call_tool("rebuild_and_rollout", {})
stream_id = result["progress_stream_id"]
# Consume the stream via orchestrator/routes/progress.py
```

**Example MCP call** (blocking):

```python
result = await mcp.call_tool("rebuild_and_rollout", {"wait": True})
assert result["exit_code"] == 0
assert "egg-orchestrator" in result["rolled_out_images"]
```

**Integration test**: `integration_tests/test_k8s_deployment_tools.py`
exercises this tool end-to-end — tagged `@pytest.mark.slow` and gated
behind `EGG_INTEGRATION_REBUILD=1` so the default `make test` invocation
does not rebuild images unless explicitly requested.

## `get_service_logs`

Read logs from the pod(s) backing the `gateway` or `orchestrator`
Deployment. Complements `get_container_logs`, which covers agent-sandbox
containers only. Added in
[#1853](https://github.com/jwbron/egg/issues/1853) after a pipeline failed
with three agents hitting "Connection refused" at the gateway: operators
had no in-MCP way to confirm whether the gateway was still coming up, not
crashing, or refusing requests for some other reason, and had to shell
into the cluster (`kubectl logs -n egg-system deploy/gateway`) to
self-serve.

**HTTP route**: `GET /api/v1/deployment/logs`

**Input schema**:

```json
{
  "service": "gateway",
  "lines": 100,
  "since_seconds": 600
}
```

- `service` is required and allowlisted to `"gateway"` or `"orchestrator"`.
  Agent-pod logs live in the `egg-agents` namespace and are already
  exposed through `get_container_logs`; keeping this endpoint bounded
  avoids it turning into a generic kubectl-logs proxy.
- `lines` defaults to 100 and is capped at 10 000 — enough for real
  diagnostic scrollback without unbounded response sizes.
- `since_seconds` (optional) scopes the read to "logs newer than N
  seconds ago" — useful for "logs around when my pipeline failed at
  HH:MM."

**Output shape**:

```json
{
  "service": "gateway",
  "namespace": "egg-system",
  "pods": [
    {
      "pod": "gateway-7d4b9c5f9-abcde",
      "logs": "INFO ... listening on :9848\n..."
    },
    {
      "pod": "gateway-7d4b9c5f9-fghij",
      "logs": "",
      "error": "Failed to read logs: container in CrashLoopBackOff"
    }
  ]
}
```

Replicas >1 are each returned as their own `{pod, logs}` entry so the
operator can tell which chunk came from where. Pods that encounter a
transient non-404 failure (kubelet timeout, `CrashLoopBackOff` with no
logs yet, etc.) include an `"error"` key so operators see partial
results from healthy replicas. A pod that vanishes between the selector
listing and the log read is skipped entirely.

**Error shapes**:

- `404` — the Deployment is not present in `egg-system`, or the
  selector returned zero pods (fresh rollout still coming up).
- `500` — Kubernetes API failure (selector missing, apiserver
  unreachable).

**Non-k8s runtime**:

```json
{"error": "not_available_on_runtime", "runtime": "docker"}
```

**Example MCP call** — the concrete miss from #1853, cross-referencing
gateway logs when multiple agents hit "Connection refused" during spawn:

```python
logs = await mcp.call_tool("get_service_logs", {
    "service": "gateway",
    "lines": 200,
    "since_seconds": 300,
})
for chunk in logs["pods"]:
    print(chunk["pod"])
    if "error" in chunk:
        print(f"  [error] {chunk['error']}")
    else:
        print(chunk["logs"])
```

## See Also

- [`/deployment-diagnose` skill](../../skills/deployment-diagnose/SKILL.md) —
  composes these tools into a control-plane triage report.
- [`/agent-diagnose` skill](../../skills/agent-diagnose/SKILL.md) —
  per-agent triage that uses `validate_network_isolation` and
  `get_deployment_context`.
- [Deployment guide](../guides/deployment.md) — end-to-end deployment
  flow; links here from its operator-tooling section.
- [Deployment diagnostics guide](../guides/deployment-diagnostics.md) —
  when to use each skill, bounded-evidence caveats, and the redaction
  guarantee.
- [Kubernetes migration](../architecture/kubernetes-migration.md) —
  architecture doc that references the two diagnostic skills.
- [Network isolation](../architecture/network-isolation.md) — the expected
  allow/deny matrix asserted by `validate_network_isolation`.
- [Orchestrator CLI](orchestrator-cli.md) — `egg-orch` commands that
  complement these MCP tools.
- [Redaction](redaction.md) — base redaction patterns; the
  `orchestrator/redaction.py` helpers used by the skills are a
  focused subset.
