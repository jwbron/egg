# Deployment Diagnostics

> **Issue**: [#1759](https://github.com/jwbron/egg/issues/1759) | **Audience**: operators, on-call engineers, agent developers debugging a stuck pipeline.

This guide explains **when to use each diagnostic skill**, what evidence
each one gathers, the bounded-evidence caveats you should expect, and the
redaction guarantee that keeps skill output safe to paste into a ticket or
Slack thread.

The diagnostic surface landed as two complementary skills:

| Skill | Scope | Typical trigger |
|-------|-------|-----------------|
| [`/deployment-diagnose`](../../skills/deployment-diagnose/SKILL.md) | Control plane: `egg-orchestrator` + `egg-gateway` Deployments in `egg-system` | Control plane unreachable, unhealthy, or rolling restarts; `make deploy` seemingly succeeded but something is off |
| [`/agent-diagnose`](../../skills/agent-diagnose/SKILL.md) | A single agent Pod in `egg-agents` | An agent Pod exited non-zero and the pipeline is stuck or failed |

Both skills compose the six MCP tools from
[`docs/reference/mcp-deployment-tools.md`](../reference/mcp-deployment-tools.md)
with existing MCP primitives (`check_health`, `list_containers`,
`get_container_logs`) into a single evidence chain — so you do not have to
remember which primitive to call first, or manually run the
pattern-matched error classifier.

## When To Use Which Skill

### Use `/deployment-diagnose` when…

- `make deploy` / `make redeploy` reports success, but
  `kubectl get pods -n egg-system` shows a Pod stuck `Pending`,
  `CrashLoopBackOff`, or `ImagePullBackOff`.
- The orchestrator or gateway endpoint is unreachable
  (`orchestrator.egg-system.svc.cluster.local:9849` returns
  `connection refused`).
- You suspect **image drift** — the running image tag does not match what
  `make build` just produced (the exact class of bug that prompted
  [#1759](https://github.com/jwbron/egg/issues/1759) via
  [#1692](https://github.com/jwbron/egg/issues/1692)).
- You suspect **manifest drift** — a `kubectl edit` or overlay change
  was never `make deploy`-ed.
- NetworkPolicy enforcement is in doubt (CNI may have silently reverted to
  Flannel after a k3s restart).

Typical invocation:

```bash
/deployment-diagnose
# or, to narrow Events/log scanning to one service:
/deployment-diagnose gateway
```

The skill returns a three-section report — `### Top finding`,
`### Supporting evidence`, `### Per-primitive data` — capped at 10 MCP
primitive calls per invocation to stay inside the 30 req/min server rate
limit.

### Use `/agent-diagnose` when…

- An agent Pod exited non-zero — any role (`coder`, `tester`, `documenter`,
  `reviewer_code`, `reviewer_contract`, `overseer`, …).
- The pipeline state shows a phase stuck in `BLOCKED` / `FAILED` and you
  need to know whether the blocker was a stack trace, a missing credential,
  a NetworkPolicy deny, or a Pod-UID-vs-Job-UID asymmetry.
- You want the **pattern-matched classifier** to map the failure onto a
  known bug family (see the catalogue inlined in each skill).

Both arguments are required:

```bash
/agent-diagnose <pipeline_id> <container_id>
# Example:
/agent-diagnose issue-1759-v3 coder-abc123
```

If either argument is wrong, the skill either returns `404 on container
ID` (exactly the identifier-translation asymmetry from
[#1760](https://github.com/jwbron/egg/issues/1760) /
[#1764](https://github.com/jwbron/egg/issues/1764)) or wastes a call.
Verify the IDs via `egg-orch pipeline status <id>` or
`kubectl get pods -n egg-agents -l pipeline=<id>` first.

## What Each Skill Gathers

### `/deployment-diagnose`

1. `check_health` — orchestrator + gateway `ok` / `not_ok`.
2. `list_containers` — control-plane pod inventory (phase, restart count,
   container statuses).
3. `get_deployment_context` — runtime, namespace, CNI, NetworkPolicy
   enforcement, image tags for orchestrator / gateway / agents.
4. `validate_deployment_manifests` — five warn-on rules against the
   committed overlay (missing Secret, missing hostPath, missing image,
   selector-label mismatch, env-var collision).
5. Recent `Warning` Events in `egg-system`.
6. Mounted Secret / ConfigMap presence check (rolls into the Events
   primitive when `FailedMount` fires).
7. Applied-vs-running spec diff (bounded by the image-tag delta from
   Step 3 + overlay delta from Step 4 — not a full cluster diff).
8. Silent-failure grep across gateway logs since the **current pod
   incarnation's start** (bounded by pod lifetime, not a persistent
   aggregator; see caveats below).
9. Same pattern-matched grep against orchestrator logs when the focus hint
   includes the orchestrator.

Output contract:

```markdown
### Top finding
<one- or two-sentence root cause, keyed to the catalogue row when a
 pattern matched>

### Supporting evidence
- bullet points only, no prose
- scannable in < 15 seconds

### Per-primitive data
- full redacted tool outputs, one subsection per primitive
```

### `/agent-diagnose`

1. `list_containers` — resolve the Pod from `pipeline_id` + `container_id`.
   A miss is itself diagnostic (Pod-UID-vs-Job-UID asymmetry).
2. Job spec as submitted by `kubernetes_spawner` — image tag, env
   envelope (key names, not values), resource limits, `backoffLimit`,
   `activeDeadlineSeconds`, label selector.
3. `get_container_logs` — last 100 lines, piped through
   `redact_log_tail()` before any snippet is surfaced.
4. Recent `Warning` Events for the Pod and the Job.
5. Resolved env — piped through `redact_env()`; only key names are
   surfaced, values are masked.
6. `validate_network_isolation` — synthetic four-probe egress matrix for
   the Pod's role.
7. Pattern-matched classifier over steps 3 / 4 / 5; pick the
   highest-severity match, break ties by most-recent-event timestamp.

Output contract is identical to `/deployment-diagnose`: three sections,
redacted everywhere a snippet appears.

Both skills embed the same known-failure-mode catalogue (12 rows covering
the four bug families that recurred through the #1692 / #1759 validation
passes: identifier-translation, role/auth boundary, missing-Secret-at-boot,
NetworkPolicy regression). The catalogue is inlined in each skill for the
first ship and will be extracted to `shared/diagnostics/failure-modes.yaml`
when [#1806](https://github.com/jwbron/egg/issues/1806) lands; the two
skills must stay in lockstep until then.

## Bounded-Evidence Caveats

The diagnostic skills are *evidence gatherers*, not live debuggers. Three
caveats bound what they can see.

### Gateway log window is bounded by pod lifetime

`/deployment-diagnose`'s silent-failure grep runs against
`get_container_logs` for the gateway pod. Logs are bounded by the **current
pod incarnation's start** — if the pod restarted in the last minute,
anything from the prior incarnation is unreachable.

This is not a skill-level bug; the underlying Kubernetes log stream is
scoped that way. A persistent log aggregator that would let
`/deployment-diagnose` look further back is tracked separately in
[#1805](https://github.com/jwbron/egg/issues/1805) (log persistence for
short-lived crashed pods) and referenced by the
[#1767](https://github.com/jwbron/egg/issues/1767) silent-failure scan.

**Operator workaround**: run `/deployment-diagnose` while the pod is
still in its failed state rather than after you restart it. If the pod
auto-restarted by the time you got to it, the last redacted log snippet
may be empty — the Events and manifest-validation outputs remain the
load-bearing evidence in that case.

### Short-lived crashed pod logs are reaped quickly

`/agent-diagnose` depends on `get_container_logs` to surface the log tail.
If the Pod exited fast and Kubernetes garbage-collected it before you ran
the skill, the call returns `logs_unavailable` / `pods "<pod>" not found`.

The skill detects this case and surfaces it in the Top finding — it does
**not** silently return "no classifier match" when logs are simply gone.
You will still get the Job spec, Events, and env keys; the classifier
runs against the reduced evidence.

**Concurrent BRC phases**: For pipelines running in concurrent execution
mode, the orchestrator now captures a frozen exit snapshot
(`AgentExitInfo`) for each container as it exits and appends it to
`PhaseExecution.agent_exits`. This snapshot includes the last 200 lines
of container stdout/stderr (each capped at 4 096 chars), the exit code,
the role, and the container ID at time of exit — and it persists in
pipeline state even after the Pod is gone. Retrieve it via:

```bash
egg-orch phase get <pipeline-id>
# look for the "agent_exits" array in the phase_execution block
```

The `container_id` in each entry can also be fed directly to
`/agent-diagnose` while the Pod still exists; `last_lines` gives you the
log tail after it doesn't. This means the `short-lived pod, log
unavailable` failure class is mitigated for concurrent phases even without
a persistent log aggregator.

The broader [#1805](https://github.com/jwbron/egg/issues/1805) follow-up
(persistent log aggregation for all agent types) remains open; the
`agent_exits` snapshot only covers concurrent BRC phases.

### Cluster Event retention

Both skills read Events from the Kubernetes API. k3s defaults to **1 hour
of Event retention**; beyond that, Events are silently dropped. If your
failure is older than an hour, the skill's Event bullets will be empty.

**Operator workaround**: re-run the skill closer to the failure timestamp,
or pair it with `kubectl get events -n <ns> --sort-by=.metadata.creationTimestamp`
invocations captured in a ticket at the time of failure.

## Redaction Guarantee

Skill output is safe to paste into a ticket, Slack thread, or public PR
review. Both skills pipe every piece of free-text / env content through
the `orchestrator/redaction.py` helpers:

- `redact_log_tail(text)` — runs a Bearer-JWT regex
  (`\b(Bearer\s+)?ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`)
  plus a generic API-key-shape regex and replaces matches with
  `[REDACTED]`.
- `redact_env(env)` — masks values for keys in the
  `_PROTECTED_ENV_KEYS` denylist (the same frozenset
  `kubernetes_spawner` uses to construct Job envs), plus `*_TOKEN`,
  `*_SECRET`, `*_KEY` glob patterns (case-insensitive), plus explicit
  keys `GITHUB_TOKEN`, `GH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_API_KEY`.

Specifically, the following are guaranteed **never to appear in skill
output**:

- `EGG_LIFECYCLE_SECRET`
- `EGG_SESSION_TOKEN`
- `GITHUB_TOKEN` / `GH_TOKEN`
- `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY`
- Any environment variable whose key matches `*_TOKEN`, `*_SECRET`, or
  `*_KEY` (case-insensitive)
- Bearer JWTs (standard `ey<segment>.<segment>.<segment>` shape)
- OpenAI-style `sk-...` API keys and other generic key-shaped tokens

The redaction guarantee is asserted by a dedicated unit test
(`orchestrator/tests/test_redaction.py`) that seeds known secret values
and asserts their absence from the redacted output. If the catalogue of
protected keys grows, the test grows with it.

> **Note on reuse**: `orchestrator/redaction.py` is a *focused subset* of
> the broader redaction surface documented in
> [reference/redaction.md](../reference/redaction.md). The full redactor
> (`shared/egg_contracts/redactor.py`) handles checkpoint transcripts and
> structured dict traversal; the skill-local helpers handle log tails and
> env dicts. They share the same denylist patterns; when you extend one,
> extend both.

## Validation Flow

To validate the diagnostic surface against a fresh k3s node, run the
following sequence (mirrors the acceptance criteria from
[#1759](https://github.com/jwbron/egg/issues/1759)):

1. `make redeploy` — rebuild and roll out.
2. `/deployment-diagnose` — expect `### Top finding\nNo issues found.`
3. `kubectl scale deploy/egg-orchestrator --replicas=0 -n egg-system`.
4. `/deployment-diagnose` — expect the scale-0 condition as the Top
   finding. Confirm no raw secrets appear anywhere in the output.
5. Submit a trivial pipeline, let an agent fail (for example, submit a
   task with an intentionally bad contract), then run:
6. `/agent-diagnose <pipeline_id> <container_id>` — expect the
   pattern-matched classifier to name the failure class. Confirm the
   redacted env section shows `<key>=<redacted>` for every sensitive
   key and no raw JWT anywhere in the log tail.

The integration test `integration_tests/test_k8s_deployment_tools.py`
automates steps 1–4 end-to-end; skip it with
`EGG_RUNTIME != "kubernetes"` or opt in with `EGG_INTEGRATION_REBUILD=1`
for the slow rebuild-and-rollout path.

## Rate Limits

The MCP server is rate-limited to **30 requests per minute**. Each skill
caps itself at a known budget:

- `/deployment-diagnose` — max 10 primitive calls per invocation
  (typical 6–7).
- `/agent-diagnose` — max 8 primitive calls per invocation
  (typical 5–6).

You can safely run both skills back-to-back during a single incident
without tripping the rate limit. Chaining more than two full invocations
inside a minute may start returning 429s — space them out or narrow the
second invocation with a `[component?]` hint.

## See Also

- [MCP deployment tools reference](../reference/mcp-deployment-tools.md) —
  input/output schemas for every tool the skills call.
- [`/deployment-diagnose` skill](../../skills/deployment-diagnose/SKILL.md) —
  full evidence chain + catalogue.
- [`/agent-diagnose` skill](../../skills/agent-diagnose/SKILL.md) —
  full evidence chain + catalogue.
- [Kubernetes migration](../architecture/kubernetes-migration.md) — the
  architecture doc that links here as the operator-facing entry point
  for diagnostics.
- [Network isolation](../architecture/network-isolation.md) — the
  expected allow/deny matrix that `validate_network_isolation`
  asserts.
- [Redaction reference](../reference/redaction.md) — base redaction
  patterns; the skill-local helpers are a focused subset.
- [Deployment guide](deployment.md) — end-to-end deployment flow.
