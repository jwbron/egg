---
name: agent-diagnose
description: "Triage a single failed egg agent Pod: gathers Job spec, status, redacted logs, Events, redacted env, and an egress probe, then runs the pattern-matched error classifier."
disable-model-invocation: true
argument-hint: "<pipeline_id> <container_id>"
---

# Agent Diagnose

You are triaging a **single agent Pod** in the `egg-agents` namespace. This
skill composes existing MCP primitives — `list_containers`,
`get_container_logs` — with the #1759 deployment tools
(`get_deployment_context`, `validate_network_isolation`) and the redaction
helper (`orchestrator/redaction.py`) to produce a prioritized triage report
for a specific failed agent.

Use this skill when:

- An agent Pod exited non-zero (any agent role — coder, tester, documenter,
  reviewer_code, reviewer_contract, etc.) and the pipeline is stuck or
  failed.
- You need to know *why* the agent failed — missing credential, 403 from the
  gateway, 404 on a container-id lookup, a stack trace in the sandbox logs,
  or a NetworkPolicy block preventing the agent from reaching the gateway.

This skill is the per-agent counterpart to
[`/deployment-diagnose`](../deployment-diagnose/SKILL.md), which covers
control-plane health in `egg-system`.

## Argument Parsing

Both arguments are **required**:

| Input | Interpretation |
|-------|----------------|
| `/agent-diagnose <pipeline_id> <container_id>` | Triage the agent identified by the Pod UID / container ID inside the given pipeline. |

If either argument is missing, emit a usage message and stop:

```
Usage: /agent-diagnose <pipeline_id> <container_id>

Example: /agent-diagnose issue-1759-v3 coder-abc123
```

Do not guess — passing the wrong `pipeline_id` / `container_id` wastes the
rate-limit budget and can misattribute a failure.

## Runtime Gate

Before running any probes, call `get_deployment_context`. If the returned
`runtime` is not `"kubernetes"`, stop and emit:

```
### Top finding
This skill only runs against the Kubernetes runtime. Detected runtime: <runtime>.

### Supporting evidence
- `get_deployment_context.runtime` = <runtime>
- If you are running in Docker mode, use `list_containers` + `get_container_logs` directly.
```

## Evidence Chain

Gather evidence in this order. **Cap at 8 primitive tool calls per
invocation** — two fewer than `/deployment-diagnose` because this skill also
runs a probe Job via `validate_network_isolation`, which itself counts as one
call but kicks off cluster work that takes up to 30 s.

### Step 1 — Resolve the Pod

Call `list_containers` filtered by `pipeline_id`. Find the entry whose
container ID matches the `<container_id>` argument. Record:

- Pod name (may differ from `<container_id>` if the container ID is a Docker
  name vs. a Pod UID — see the #1760 / #1764 bug family below)
- Pod phase
- Container statuses (`ready`, `restartCount`, `state.terminated.exitCode`,
  `state.terminated.reason`, `lastState` when present)

If no match is found, the failure class is almost certainly the
**Pod-UID-vs-Job-UID asymmetry** (see catalogue row `404 on container ID`).
Stop after Step 1 and emit that as the Top finding — no further calls
needed.

*Primitive call budget: 1.*

### Step 2 — Job spec as submitted

Fetch the Job spec from `kubernetes_spawner` — either via a dedicated MCP
primitive if one exists, or via a shell-out to `kubectl get job
<job-name> -n egg-agents -o yaml` (resolved via `_resolve_job_name` if
available).

Record:

- `spec.template.spec.containers[0].image` (tag drift indicator)
- `spec.template.spec.containers[0].env[*].name` (envelope of env keys — do
  not log values; those go through Step 5 redaction)
- `spec.template.spec.containers[0].resources` (OOM diagnostic)
- `spec.backoffLimit` (how many retries before the Job is marked failed)
- `spec.activeDeadlineSeconds` (hard timeout)
- label selector and annotations (network-policy targeting)

*Primitive call budget: 1.*

### Step 3 — Pod logs (redacted)

Call `get_container_logs` with `pipeline_id` and `container_id`. Limit the
fetch to the **last 100 lines** — the pattern matchers operate on tail
signatures, not full transcripts, and the rate limit is tight.

**Pipe the returned text through `redact_log_tail()` from
`orchestrator/redaction.py` before any snippet appears in the final
report.** No raw Bearer JWTs, no `*_TOKEN` / `*_SECRET` / `*_KEY` /
`GITHUB_TOKEN` / `GH_TOKEN` / `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` values.

If the Pod is gone (short-lived crashed pod, logs already reaped), record
`logs_unavailable` and note the log-persistence follow-up (#1805). This is
one of the two bounded-evidence caveats — call it out in the Top finding
when it is the cause of a partial report.

*Primitive call budget: 1.*

### Step 4 — Recent Events for Pod + Job

Fetch Events scoped to the Pod name and Job name from Step 2. Surface the
last 10 of type `Warning`. Common high-signal Events include
`FailedScheduling`, `FailedMount`, `BackoffLimitExceeded`,
`NetworkPolicyDeny`, and image-pull failures.

*Primitive call budget: 1.*

### Step 5 — Redacted env

Fetch the resolved env for the Pod's container — this is the env as the Pod
actually saw it, which may differ from the Job spec's `env[*]` when
ConfigMaps/Secrets mutated in-flight.

**Pipe through `redact_env()` from `orchestrator/redaction.py` before any
entry appears in the report.** The helper masks values for:

- keys in the base `_PROTECTED_ENV_KEYS` denylist (reused from
  `kubernetes_spawner`)
- glob patterns `*_TOKEN`, `*_SECRET`, `*_KEY` (case-insensitive)
- explicit keys `GITHUB_TOKEN`, `GH_TOKEN`, `ANTHROPIC_API_KEY`,
  `CLAUDE_API_KEY`

Surface only **key names** and a `value=<redacted>` marker. Never surface
a raw value — even if you think it is safe.

*Primitive call budget: 1 if a dedicated primitive exists; 0 if derived
from Step 2's Job spec + live ConfigMap reads.*

### Step 6 — Synthetic egress probe

Call `validate_network_isolation(pipeline_id=<pipeline_id>, role=<role>)`
where `<role>` is inferred from the Pod labels in Step 1 (default `coder`
if unclear).

Record the returned map:

- `gateway_reachable` (expect `true`)
- `internet_blocked` (expect `true`)
- `agent_pods_unreachable` (expect `true`)
- `orchestrator_api_reachable` (expect `true`)

Any deviation is a NetworkPolicy drift — flag it high-severity in the Top
finding. The probe runs in a throwaway Job with
`ttlSecondsAfterFinished=0`, so it self-cleans.

Skip this step if `get_deployment_context.network_policy_enforcement` is
falsy — the probe returns `network_policy_enforcement_not_detected` and
wastes a call. Fall back to recording the CNI value from Step 3 of
`/deployment-diagnose`'s evidence chain (if you ran it in the same
session) or the current `get_deployment_context` call.

*Primitive call budget: 1.*

### Step 7 — Cross-check against the known-failure-mode catalogue

Run the pattern matcher below against the redacted log text from Step 3,
the redacted env keys from Step 5, and the Event messages from Step 4.
Each matching row yields a candidate finding; the Top finding is the
highest-severity match, broken by most-recent-timestamp for ties.

*Primitive call budget: 0 (pure in-process pattern matching).*

## Known-Failure-Mode Catalogue (inlined)

This catalogue is inlined for the first ship and must stay in lockstep with
the one in [`/deployment-diagnose`](../deployment-diagnose/SKILL.md). Both
skills will reference `shared/diagnostics/failure-modes.yaml` when #1806
lands.

| Pattern (regex, case-insensitive) | Failure class | Next step |
|-----------------------------------|---------------|-----------|
| `403.*from gateway` / `HTTP 403.*gateway` | Role/auth boundary mismatch (#1766 family) | Verify the agent role matches the allowed fine/coarse role set for the requested endpoint; check `sandbox/egg_restrictions/`. |
| `404.*container[_ ]id` / `404 on container ID` | Pod-UID vs Job-UID asymmetry (#1760 / #1764 family) | The caller is passing a Pod UID where a Job UID is expected (or vice versa). Use `_resolve_job_name()` on the lookup path. |
| `log[s]? unavailable.*pod not found` / `pods "<pod>" not found` | Short-lived crashed pod, logs reaped (#1805 pending) | Partial report; request a re-run after enabling log persistence, or catch the pod while it is still Running. |
| `connection refused.*gateway.egg-system` | Gateway Service unreachable | Check the gateway Pod is `Running` and the `egg-gateway` Service has endpoints. |
| `connection refused.*orchestrator.egg-system` | Orchestrator Service unreachable from agent | Check NetworkPolicy egress from `egg-agents` — should allow egress to `egg-system/gateway` only, not direct to orchestrator. |
| `Unauthorized.*lifecycle.secret` / `401.*EGG_LIFECYCLE_SECRET` | #1769 regression (missing or misrouted lifecycle secret) | Confirm the Secret is mounted on the agent Pod and the env var name matches the orchestrator's expectation. |
| `OOMKilled` / `container was OOMKilled` | Memory limit too low | Raise `resources.limits.memory` on the Job spec; most coder Jobs need ≥ 1Gi. |
| `DeadlineExceeded` / `activeDeadlineSeconds.*exceeded` | Agent exceeded its hard timeout | Either the agent genuinely hung (investigate logs) or the deadline is too short — check `spec.activeDeadlineSeconds`. |
| `BackoffLimitExceeded` | Job retried past backoff limit | The root cause is whatever was logged on the final retry — re-run this skill against the final Pod ID. |
| `ImagePullBackOff` / `ErrImagePull` on agent Pod | Sandbox image missing in containerd | `make build` + `k3s ctr images import` the sandbox image. |
| `FailedMount.*secret "<name>"` | Secret missing at Pod admission | Create the referenced Secret, then re-submit the task. |
| `NetworkPolicy.*deny` (from Calico felix logs in events) | NetworkPolicy blocked the required connection | Check `k8s/base/network-policies.yaml` and the per-role label selectors. |

## Output Contract

The report has exactly three sections, in this order:

```markdown
### Top finding

<One- or two-sentence description of the matched failure class. Cite the
regex pattern and the #nnnn bug-family tag when the match is in the
identifier-translation or role/auth boundary cluster.>

### Supporting evidence

- Pod: `<pod-name>` phase=<phase>, exitCode=<N>, restartCount=<N>
- Job: `<job-name>`, image=`<image-tag>`, backoffLimit=<N>, activeDeadlineSeconds=<N>
- Recent Warning events (<N>): <top 3 summarized>
- Log tail matches: `<pattern>` → `<redacted snippet>` (line <N>)
- Env keys present (<N>): <key1>, <key2>, <key3>, ... (all values redacted)
- Egress probe: gateway_reachable=<bool>, internet_blocked=<bool>, agent_pods_unreachable=<bool>, orchestrator_api_reachable=<bool>
- Pattern classifier: <matched class> / `<regex>` → <next step from catalogue>

### Per-primitive data

<Full raw output for each primitive, redacted. One ### subsection per
primitive. Include skip notes for any step that was skipped due to the
8-call cap or because the Pod disappeared mid-run.>
```

The **Top finding** is at most three sentences and keyed to a catalogue row
when a pattern fired. Break ties by severity (mount failure > NetworkPolicy
deny > OOM > crash > non-zero exit without classifier match) then by most
recent event timestamp.

When no pattern matches, the Top finding is:
`"No classifier match. Exit code was <N>. Inspect per-primitive data for the full redacted log tail."`

The **Supporting evidence** section is bullet points only. The
**Per-primitive data** section holds the full redacted tool outputs.

## Rate-Limit Budget

Maximum 8 primitive MCP calls per invocation. Typical first-pass budget:

- Step 1: 1 (`list_containers`)
- Step 2: 1 (Job spec fetch)
- Step 3: 1 (`get_container_logs`)
- Step 4: 1 (Events fetch)
- Step 5: 1 (env fetch — when a dedicated primitive exists)
- Step 6: 1 (`validate_network_isolation`)

That is 6 calls, leaving 2 headroom for follow-up Pod-log calls (e.g.
fetching a longer window if the 100-line tail missed the error line, or
re-running `list_containers` after an operator-applied fix).

## Security Guarantees

- All log text passes through `redact_log_tail()` before any snippet
  appears in the report.
- All env entries pass through `redact_env()`; only key names are surfaced,
  with `value=<redacted>` markers.
- The egress probe Job (`validate_network_isolation`) is spawned with
  `automountServiceAccountToken=false`, the `_PROTECTED_ENV_KEYS` denylist
  applied, `ttlSecondsAfterFinished=0`, and
  `activeDeadlineSeconds=30` — it cannot leak the caller's credentials and
  it self-cleans within 30 seconds.
- No arbitrary shell execution inside the agent Pod — this skill reads
  evidence, it does not exec into the container.

## Bounded-Evidence Caveats

- **Short-lived crashed pods**: if the Pod has been garbage-collected, its
  logs are unreachable. The classifier will surface `logs_unavailable` and
  reference #1805 (log-persistence follow-up). Partial reports in this
  case are expected.
- **Cluster event retention**: Events older than the cluster's retention
  window (k3s default: 1 hour) will not appear. Re-run the skill closer
  to the failure timestamp when feasible.
- **Network probe caveat**: `validate_network_isolation` tests the
  *currently-applied* NetworkPolicy set. If the policy was changed after
  the agent failed, the probe reflects the current state, not the state
  at failure time.

## See Also

- [MCP deployment tools reference](../../docs/reference/mcp-deployment-tools.md)
- [Deployment diagnostics guide](../../docs/guides/deployment-diagnostics.md)
- [`/deployment-diagnose`](../deployment-diagnose/SKILL.md) — control-plane
  diagnostics
- [Network isolation](../../docs/architecture/network-isolation.md) —
  expected allow/deny matrix for the egress probe
- [Redaction](../../docs/reference/redaction.md) — shared redaction
  patterns (the skill-local `redact_log_tail` / `redact_env` are a focused
  subset)
