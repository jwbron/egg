---
name: deployment-diagnose
description: "Diagnose egg control-plane (orchestrator + gateway) deployment issues on Kubernetes. Gathers prioritized evidence via MCP tools and produces a ranked findings report."
disable-model-invocation: true
argument-hint: "[component?]"
---

# Deployment Diagnose

You are producing a prioritized, human-readable diagnostic report for the egg
**control plane** (`egg-system` namespace: `egg-orchestrator`, `egg-gateway`).
This skill composes existing MCP primitives — `check_health`,
`list_containers`, `get_container_logs` — with the new deployment tools from
#1759 (`get_deployment_context`, `validate_deployment_manifests`) into a single
evidence chain that mirrors the manual `kubectl` walk an operator would do
during a fresh-node validation pass.

Use this skill when:

- `make deploy` / `make redeploy` succeeded but the control plane is
  unreachable, unhealthy, or rolling crash-loops.
- You suspect stale images, missing secrets, manifest drift, or
  NetworkPolicy/CNI misconfiguration.
- An operator needs a single report — **not** a raw `kubectl describe` dump —
  with the top finding surfaced first.

This skill is the operator-facing counterpart to
[`/agent-diagnose`](../agent-diagnose/SKILL.md), which triages individual
agent-pod failures inside `egg-agents`.

## Argument Parsing

Parse the optional `[component?]` argument:

| Input | Interpretation |
|-------|----------------|
| `/deployment-diagnose` | Diagnose both orchestrator and gateway (default). |
| `/deployment-diagnose orchestrator` | Focus evidence gathering on `egg-orchestrator` only. |
| `/deployment-diagnose gateway` | Focus evidence gathering on `egg-gateway` only. |
| `/deployment-diagnose <anything-else>` | Treat as free-text hint; still scan both but prioritize matches in findings. |

The component filter is a **focus hint**, not a hard filter — you still call
`get_deployment_context` (which covers the whole cluster) and
`check_health` (which covers both services). It narrows the Events and
log-scan surfaces only.

## Runtime Gate

Before running any probes, call `get_deployment_context`. If the returned
`runtime` is not `"kubernetes"`, stop and emit:

```
### Top finding
This skill only runs against the Kubernetes runtime. Detected runtime: <runtime>.

### Supporting evidence
- `get_deployment_context.runtime` = <runtime>
- If you are running in Docker mode, use `check_health` and `list_containers` directly.
```

This mirrors the runtime gate on the underlying deployment tools (see
[MCP deployment tools reference](../../docs/reference/mcp-deployment-tools.md)).

## Evidence Chain

Gather evidence in this order. **Cap at 10 primitive tool calls per
invocation** — the MCP server is rate-limited to 30 req/min and this skill
must coexist with whatever other work the session is doing. If a step would
exceed the cap, skip it and note the skip in the final report.

### Step 1 — Health

Call `check_health`. Record `ok`/`not_ok` status for both `orchestrator` and
`gateway`, plus any status detail the endpoint returns.

*Primitive call budget: 1.*

### Step 2 — Control-plane pod inventory

Call `list_containers` with scope limited to the `egg-system` namespace
(using whatever filter the tool exposes; if it does not, accept the full list
and filter the results).

For each matching pod, record:

- pod name
- phase (`Running`, `Pending`, `CrashLoopBackOff`, etc.)
- container statuses (`ready`, `restartCount`, `lastState.terminated.reason`
  when present)
- node name (useful when one node is wedged)

*Primitive call budget: 1.*

### Step 3 — Deployment context + image drift check

Call `get_deployment_context` with no arguments. Record:

- `runtime`
- `kubeconfig_context`
- `namespace` (expected: `egg-system`)
- `cni` and `network_policy_enforcement`
- `images.orchestrator`, `images.gateway`, `images.agents`

Compare the image tags against what the running Pods report (from Step 2's
container statuses, or the `images` key on the pod spec if the list
primitive exposes it). Flag any mismatch — an image tag that differs from
the rolled-out tag is the exact drift that tanked the #1692 / #1759
validation session.

*Primitive call budget: 1.*

### Step 4 — Manifest validation

Call `validate_deployment_manifests` with no overlay argument (default
`overlay_path` will pick the committed overlay). Record:

- the full `warnings` list
- for each warning: `rule`, `severity`, `resource`, `message`

On non-k3s clusters, warnings flagged `{"skipped": "not_k3s", ...}` are
informational — surface them in Supporting evidence but do not weight them
into the top finding.

*Primitive call budget: 1.*

### Step 5 — Recent Warning events

If `egg-orch` / MCP exposes an events primitive, use it. Otherwise emit a
short `kubectl get events -n egg-system --field-selector type=Warning
--sort-by=.metadata.creationTimestamp` invocation via the shell tool and
capture the last 20 Warning events.

Prioritize events whose `involvedObject` matches the component filter.

*Primitive call budget: 1.*

### Step 6 — Mounted Secret / ConfigMap presence

For each control-plane pod, verify the Secrets and ConfigMaps it references
actually exist. The deployment manifest references (expected):

- `egg-lifecycle-secret` (mounted on orchestrator + gateway)
- `egg-gateway-session-tokens` (gateway only)
- `egg-orchestrator-config`, `egg-gateway-config` (ConfigMaps)

Missing objects are a common silent-failure mode: the pod may Pending forever
without a clear Event. Use the events primitive from Step 5 — it will surface
`FailedMount` entries when a Secret is missing.

*Primitive call budget: 1 if a dedicated primitive exists; 0 if rolled into
Step 5.*

### Step 7 — Applied-vs-running spec diff

Compare each Deployment's `spec` (as rendered from the committed overlay)
against the live `Deployment` object. Use `validate_deployment_manifests`
output plus `get_deployment_context.images` to bound the diff — full
`kubectl diff` against a production cluster is out of scope for this skill.

Flag any of:

- image tag drift (already captured in Step 3)
- replica count mismatch
- env var additions/removals
- volume/volumeMount additions/removals

*Primitive call budget: 1.*

### Step 8 — Silent-failure grep across gateway logs

Call `get_container_logs` for the gateway pod identified in Step 2. Bound
the scan to **the current pod incarnation's lifetime** — do not rely on a
log-persistence aggregator that does not exist yet (tracked as #1805 and
#1767).

Pipe the returned text through the redaction helper (`redact_log_tail` from
`orchestrator/redaction.py`) before surfacing any snippet in the report.
**No raw secrets in skill output.** Specifically, no Bearer JWTs, no
values for keys matching `*_TOKEN` / `*_SECRET` / `*_KEY`, no
`GITHUB_TOKEN` / `GH_TOKEN` / `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY`.

Grep the redacted text for silent-failure patterns (catalog below). Record
any match with line number and the redacted snippet.

*Primitive call budget: 1 log call + up to 2 additional if both orchestrator
and gateway are in scope.*

### Step 9 — Orchestrator logs (if the component filter demands it)

Same as Step 8 but against the orchestrator pod. Skip when `component =
gateway` and Step 8 alone already covered the suspected surface.

*Primitive call budget: 1.*

## Known-Failure-Mode Catalogue (inlined)

This catalogue is inlined in the skill for the first ship. It will be
extracted to `shared/diagnostics/failure-modes.yaml` when #1806 lands and
both skills will reference the shared file. Update both skills in lockstep
until that happens.

| Pattern (regex, case-insensitive) | Failure mode | Next step |
|-----------------------------------|--------------|-----------|
| `FailedMount.*secret "egg-lifecycle-secret"` | Lifecycle secret missing | `kubectl create secret generic egg-lifecycle-secret --from-literal=token=...` |
| `ImagePullBackOff` / `ErrImagePull` | Image missing in containerd | `make build && make redeploy` or `k3s ctr images import` |
| `CrashLoopBackOff` on orchestrator | Orchestrator boot-loop | Inspect first 100 lines of log for the actual stack |
| `CrashLoopBackOff` on gateway | Gateway boot-loop | Same; also check gateway session token Secret |
| `bind: address already in use` | Port conflict (9848/9849/9851) | Another process squatting the port; inspect Service + host |
| `x509: certificate signed by unknown authority` | TLS / CA trust drift | Check kubeconfig context and cluster-ca-cert |
| `connection refused.*orchestrator.egg-system` | Orchestrator Service unreachable | Check Service endpoints + Pod readiness |
| `connection refused.*gateway.egg-system` | Gateway Service unreachable | Same; also check NetworkPolicy egress from egg-agents |
| `EGG_ORCHESTRATOR_URL.*not set` | #1759-era config miss | Ensure the env var is in the orchestrator ConfigMap |
| `NetworkPolicy.*not enforced` / no CNI policy logs | CNI not enforcing | `kubectl get pods -n kube-system -l app.kubernetes.io/name=cilium-agent`; fall back to `make k3s-setup` |

The `#1767 silent-failure scan` from the plan maps onto the
`connection refused.*` rows plus `FailedMount.*` and
`ImagePullBackOff` / `ErrImagePull` rows above — these are the patterns that
surfaced during the #1692 / #1759 validation pass.

## Output Contract

The report has exactly three sections, in this order:

```markdown
### Top finding

<One- or two-sentence description of the single most likely cause, keyed to
the catalogue row when a pattern match fired, or "no issues found" when all
nine steps returned clean.>

### Supporting evidence

- `check_health`: orchestrator=<ok|not_ok>, gateway=<ok|not_ok>
- `get_deployment_context`: runtime=<runtime>, namespace=<ns>, images=<summary>
- `validate_deployment_manifests`: <N> warnings (`<rule>`: `<severity>` — <resource>)
- Control-plane pods: <pod>=<phase>, restartCount=<N>
- Recent Warning events (<N>): <summary of top 3>
- Log scan matches: `<pattern>` at <line> → `<redacted snippet>`
- Image drift: <yes|no>; if yes, <manifest-tag> vs <running-tag>

### Per-primitive data

<Full raw output for each primitive, redacted. One ### subsection per
primitive that fired. Include skip notes for any step that was skipped due
to the 10-call cap.>
```

The **Top finding** section is at most three sentences. If pattern matching
hit multiple rows, pick the one with the highest severity (missing Secret >
crash loop > image drift > unauth > cosmetic). If no patterns matched, the
Top finding is `"No issues found. All nine evidence steps returned clean."`.

The **Supporting evidence** section is bullet points only — no prose — so
the operator can scan it in under 15 seconds.

The **Per-primitive data** section is where raw tool output lives. Always
redacted.

## Rate-Limit Budget

Maximum 10 primitive MCP calls per invocation. Reserve one call for the
final re-check after the operator applies a fix — do not spend the full
budget on first scan. If the first-pass budget is:

- Step 1: 1 (`check_health`)
- Step 2: 1 (`list_containers`)
- Step 3: 1 (`get_deployment_context`)
- Step 4: 1 (`validate_deployment_manifests`)
- Step 5: 1 (events — merged with Step 6)
- Step 7: 0 (rolled into Step 3 + Step 4)
- Step 8: 1 (gateway logs) + 1 (orchestrator logs when in scope)

That is 7 calls worst-case, leaving 3 headroom for follow-up scans.

## Security Guarantees

- All log text passes through `redact_log_tail()` before any snippet appears
  in the report.
- No environment variable values are read or emitted — env inspection belongs
  to `/agent-diagnose`, not this skill.
- No probe Jobs are spawned — this skill is read-only. Use
  `validate_network_isolation` explicitly when you need the probe.

## Bounded-Evidence Caveats

- Gateway/orchestrator log scans are bounded by the **current pod
  incarnation's start**. If the pod restarted recently, logs from the prior
  incarnation are unavailable (see #1805 for the log-persistence follow-up).
- Event scans are bounded by the cluster's retention for the `egg-system`
  namespace (k3s default: 1 hour). Older Warning events will not appear.
- `validate_deployment_manifests` runs against the committed overlay, not
  the cluster's live state — combine with Step 7 (applied-vs-running diff)
  to detect manual `kubectl edit` drift.

## See Also

- [MCP deployment tools reference](../../docs/reference/mcp-deployment-tools.md)
- [Deployment diagnostics guide](../../docs/guides/deployment-diagnostics.md)
- [`/agent-diagnose`](../agent-diagnose/SKILL.md) — per-agent-pod triage
- [Deployment guide](../../docs/guides/deployment.md) — `make deploy` /
  `make redeploy` reference
