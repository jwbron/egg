# Analysis: MCP tooling gaps for the new Kubernetes deployment

> Issue: #1759 | Phase: refine

## Problem Statement

Validating #1692 (Docker → k3s migration) on a fresh machine surfaced a long list
of deployment bugs that the existing MCP surface gave almost no help diagnosing.
Every iteration in that session fell back to raw `kubectl` (pods, describe, logs,
rollout restart) and manual `docker build && k3s ctr images import`. The author
also notes that the validation pass for *this very issue* surfaced six more
bugs (#1760, #1764, #1765, #1766, #1767, #1768, #1769) that cluster into two
recurring shapes:

- **Identifier-translation asymmetries** (#1760, #1764) — the same Job-UID-vs-Pod-UID
  bug appearing on different surfaces.
- **Role / auth boundary errors** (#1766, #1768, #1769) — a caller reaches an
  endpoint it should not, and the failure is either silent or cryptic.

The request is to close the pipeline-centric blind spot in the MCP surface by
adding **five MCP tools** (deterministic k8s-facing actions) and **two skills**
(diagnostic workflows that compose those tools + existing primitives + judgment):

**Tools** — `validate_deployment_manifests`, `validate_network_isolation`,
`prune_stale_worktrees`, `get_deployment_context`, and a
deferred-pending-#1763 `rebuild_and_rollout`.

**Skills** — `deployment-diagnose` ("what's wrong with the control plane?"),
`agent-diagnose` ("why did this agent pod fail?").

The issue also documents (without proposing a fix) the `submit_task`
task-id-reuse papercut, and surfaces a log-persistence gap for short-lived
crashed pods that makes `agent-diagnose` less useful than it could be.

The desired outcome is that the next validation session (or any future
"egg on fresh k3s node" deployment) is bounded by the actual bugs rather
than by our inability to surface the evidence.

## Current Behavior

**MCP surface today** (`orchestrator/mcp_tools.py:64-669`) — 23 tools covering
pipeline CRUD, HITL decisions, per-agent container logs, BRC consensus status,
phase management, checkpoints, health. Everything is pipeline-centric: tools
assume the orchestrator+gateway are already up, have registered a session, and
are talking to each other. The closest thing to a "k8s tool" today is
`check_health` (`mcp_tools.py:672–767`), which hits `/health` on both services
and returns ok/not-ok — no diagnostic breadth.

**Tool pattern** — Schemas live as dicts in `PIPELINE_TOOLS`
(`orchestrator/mcp_tools.py:64-669`); handlers live as `_handle_<tool>` methods
on `PipelineToolHandler` (line 672+); each handler proxies to an orchestrator
HTTP route under `orchestrator/routes/`. The MCP server itself
(`orchestrator/mcp_server.py`) is an in-process FastMCP sidecar inside the
orchestrator Deployment, rate-limited at 30 req/min, using streamable-HTTP
transport. Adding a tool is: add schema to `PIPELINE_TOOLS`, add handler on
`PipelineToolHandler`, wire dispatch at `handle_tool_call`, add orchestrator
route if new logic is needed, add unit test that mocks `_make_request()`.

**Skills surface today** — three skills under `skills/`: `sdlc/SKILL.md`,
`babysit-pr/SKILL.md`, `egg-setup/SKILL.md`. Each is a YAML-frontmatter markdown
file with `name`, `description`, `disable-model-invocation`, `argument-hint`.
They are filesystem-discovered (no registry file). All three are effectively
orchestrator-side today — there is no operator-vs-agent split yet (#1765 is
the flip-side concern for sandbox agents).

**Kubernetes client** (`orchestrator/kubernetes_client.py`) — official
`kubernetes` python client, in-cluster auth via service account, singleton
accessor `get_kubernetes_client()` (lines 943–974). Shared by `kubernetes_spawner.py`
(Job spawn + gateway session registration) and `kubernetes_monitor.py` (pod
health, orphan cleanup). The Pod-UID-vs-Job-UID translation lives in
`_resolve_job_name()` (`kubernetes_client.py:867–931`); this is exactly the
surface #1760/#1764 reference. New k8s-facing MCP tools can reuse this singleton
rather than re-establishing auth.

**Runtime detection** — `orchestrator/routes/pipelines.py:390` reads
`EGG_RUNTIME` env (default `docker`) and returns `KubernetesSpawner` or
`ContainerSpawner`. Container spawner is aliased for backwards compatibility
so old imports still work. Namespace defaults: orchestrator/gateway in
`egg-system`, agent Jobs in `egg-agents`.

**Kustomize layout** — `k8s/base/` has `gateway-deployment.yaml`,
`orchestrator-deployment.yaml`, `gateway-service.yaml`, `orchestrator-service.yaml`,
`network-policies.yaml` (deny-all + allow-specific for agents→gateway,
agents→orchestrator, agents→kube-dns), `namespaces.yaml`, `rbac.yaml`, plus
one local overlay under `k8s/overlays/local/`. This is the canonical surface
`validate_deployment_manifests` would walk.

**Worktree lifecycle** — `gateway/worktree_manager.py` hardcodes
`WORKTREE_BASE_DIR = /home/egg/.egg-worktrees` (line 47) and owns
`create_worktree`, `delete_worktree`, `list_worktrees`,
`cleanup_orphaned_worktrees`. The gateway pod mounts that path via hostPath.
Both the gateway and the orchestrator know the path; no new configuration
is needed for `prune_stale_worktrees`.

**Network isolation verification today** — `integration_tests/test_network_isolation.py`
and `integration_tests/test_network_security.py` probe egress but against
the Docker-era network model. `k8s/base/network-policies.yaml` defines the
k8s equivalent (default-deny + allow-specific) but there is no runtime
post-deploy probe that verifies Calico is actually enforcing them. This
matches the issue's observation that "nothing in the current test plan
actually verifies it at runtime."

**Container/log diagnosis** — `list_containers` and `get_container_logs`
already work against both backends via `_get_monitor()` dispatch. The
Pod-UID ↔ Job-name translation in `kubernetes_client._resolve_job_name()`
is the exact seam #1760/#1764 collided with. `agent-diagnose` would compose
these rather than re-implement.

**Also found in situ while writing this analysis**: the gateway's contract-API
proxy defaults to `http://egg-orchestrator:9849` (`gateway/contract_api.py:62`)
but the deployed k3s Gateway Deployment does not set `EGG_ORCHESTRATOR_URL`
to the real hostname `orchestrator.egg-system.svc.cluster.local`. Every
contract read/write from the sandbox fails with "Orchestrator unreachable
— try again," so `egg-contract add-decision` and `egg-contract add-feedback`
did not work in this pipeline and I had to fall back to direct orchestrator
POSTs. This is exactly the class of bug `validate_deployment_manifests`
(missing env) and `get_deployment_context` (what image/env is the pod
actually running?) are supposed to catch. I am **not** proposing to fix
this gateway bug inside #1759, but I am flagging it so the plan author
(and the reviewer) can decide whether to open a separate tracking issue
rather than let it block the next refine/plan agent in the pipeline.

## Constraints

- **No new authentication surface** — all proposed MCP tools run inside the
  orchestrator's existing MCP server (`orchestrator/mcp_server.py`). The
  authenticated path is already the MCP request; no per-tool auth decisions
  are needed. Tools that reach into the cluster use the existing in-cluster
  ServiceAccount via `get_kubernetes_client()`.
- **Dual-runtime compatibility** — the orchestrator still needs to run
  against Docker for local dev (`EGG_RUNTIME=docker`). Proposed k8s tools
  must either (a) return a structured "not available on this runtime"
  error, or (b) degrade gracefully. `get_deployment_context` is already
  multi-runtime by design; the other four are k8s-specific.
- **Rate limits** — the MCP server is rate-limited at 30 req/min. A
  `deployment-diagnose` skill that fires many tool calls in sequence needs
  to bound itself (or the plan phase needs to lift the limit for human-run
  skills). Not a blocker, but worth naming.
- **Probe pod side effects** — `validate_network_isolation` spawns a probe
  Job in `egg-agents`. That namespace is governed by NetworkPolicies; the
  probe needs to be short-lived, garbage-collected after read, and not
  collide with an active pipeline's agents. Implementation details but
  scope-affecting: see Decision 6.
- **Host filesystem writes** — `prune_stale_worktrees(dry_run=false)`
  mutates a hostPath volume (the shared worktree dir) on whichever node
  the gateway runs on. Destructive by design; the `dry_run` flag mitigates
  but does not fully de-risk. See Feedback Q7.
- **Skill discoverability** — skills are filesystem-scanned. There is no
  harness-side registry change needed to introduce new skill dirs, but any
  operator-vs-agent split (Decision 2) requires moving files and may
  require a harness-side update that is out of band from this issue.
- **Log persistence** — `agent-diagnose` is only as useful as the log data
  it can still reach. Short-lived crashed pods (<10s) are deleted before
  `kubectl logs` can pull anything. The issue flags this as a separate
  concern; see Decision 5.
- **Blocked-on-other-work** — `rebuild_and_rollout` is explicitly deferred
  pending #1763 (SHA-tagged images). Decision 4 is whether it belongs in
  #1759 at all.
- **Related issue cluster to track** — #1760, #1764, #1765, #1766, #1767,
  #1768, #1769, #1763 are all cited by #1759 as motivation or
  prerequisite. The plan/implement phase needs to decide which of these
  land first and which #1759's skills need to assume are already fixed.
- **Write boundaries (gateway-enforced)** — the implementer role is scoped to
  `{orchestrator/,sandbox/,gateway/,k8s/,docs/,tests/,integration_tests/,skills/}`.
  No part of #1759's proposal crosses that boundary.

## Options Considered

### Option A: Ship everything in #1759 as a single umbrella

**Approach**: One plan, one implementation phase, lands all five MCP tools
(minus `rebuild_and_rollout`, deferred per Decision 4) and both skills
together. Existing patterns are reused throughout: new tool schemas added
to `PIPELINE_TOOLS`, new handlers on `PipelineToolHandler`, new orchestrator
routes where needed, skills added as new directories under `skills/`.

**Pros**:
- The issue is explicitly framed as a unified story ("the same two failure
  shapes keep recurring; tools + skills complete the story"). Keeping them
  together preserves that narrative in the commit log and review.
- The skills compose the tools. Splitting them forces the skills to be
  written against speculative tools (or delivered as no-ops and patched
  later), which is lower-quality review.
- A single plan phase can own acceptance criteria like "one end-to-end
  re-run of the #1692 validation session using only the new tools/skills,"
  which is the actual success metric. Splitting dilutes that.

**Cons**:
- Larger diff, longer review. Historical pattern at egg is that large
  umbrella issues sometimes stall in review.
- Internal dependencies between items (e.g., `deployment-diagnose` references
  `get_deployment_context`) are real but small; most items are independently
  shippable, so bundling forgoes parallelism.
- One failed acceptance criterion blocks the whole PR.

### Option B: Split by shape — MCP tools first, skills as follow-up

**Approach**: Land the four MCP tools (validate_deployment_manifests,
validate_network_isolation, prune_stale_worktrees, get_deployment_context)
in #1759's PR; open a follow-up issue for the two skills once the tools
land. The skills then compose stable primitives instead of moving targets.

**Pros**:
- Tools are deterministic, small, and unit-testable; they land fast.
- Skills can be authored after tool shapes are validated in real use —
  reduces the risk that a skill prescribes an evidence chain the tools
  can't supply.
- Smaller PRs, smaller reviews, less coupling between reviewers.

**Cons**:
- Forfeits the "close the validation loop" story. The whole point of
  the issue is that tools alone didn't help us in the #1692 validation
  session; we need the skills layer to translate evidence into prioritized
  action.
- Two issues to track; higher coordination cost across two refine/plan cycles.
- Skills work sits exposed as "future work" for an unbounded time.

### Option C: Split per-item — open child issues for each tool and skill

**Approach**: Close #1759 as a tracker; open one issue per tool and per
skill. Each child has its own full SDLC pipeline.

**Pros**:
- Maximum review granularity; each item gets its own acceptance criteria.
- Parallelism across independent items.

**Cons**:
- Enormous coordination overhead — 7 issues for ~7 small-to-medium deliverables.
- The two skills overlap heavily in evidence-gathering (pod describe,
  events, logs, env). Splitting them into separate pipelines duplicates
  design work and yields inconsistent output formats.
- The unified validation-retrospective framing is lost entirely.

### Option D: Tools now, skills blocked on log persistence

**Approach**: Land the four MCP tools in #1759. Defer *both* skills
until a separate log-persistence effort (preserve failed pods / stream
agent logs out-of-band) lands, on the grounds that `agent-diagnose`
is significantly less useful without reachable logs for short-lived pods.

**Pros**:
- Avoids shipping `agent-diagnose` as a half-measure against the very
  failure mode (short-lived crashed pods) that motivates it.
- Forces the log-persistence question to be resolved explicitly.

**Cons**:
- `deployment-diagnose` doesn't depend on log persistence — holding it
  back is conservative in the wrong direction.
- `agent-diagnose` is *still* useful for pods that don't die in <10s
  (the majority). Waiting for log persistence delays shipping value
  that already exists.

## Recommended Approach

**Option A — ship the full umbrella in #1759**, with the following carve-outs:

1. **Drop `rebuild_and_rollout` from scope** (Decision 4 defaults toward
   removal). Its only independent value is over `make redeploy`, and once
   #1763 lands that's a trivial MCP wrapper.
2. **Defer the `submit_task` task-id-reuse papercut** (Decision 3 defaults
   toward defer). It is adjacent to but not core to the tooling-gap story.
3. **Keep skills in scope but cap their ambition to reachable evidence**
   (Decision 5 defaults toward defer). `agent-diagnose` ships using
   `list_containers` / `get_container_logs` / events / env today; the
   log-persistence fix is a separate issue that will make the skill more
   useful without requiring a skill re-write.
4. **Scope the gateway→orchestrator `EGG_ORCHESTRATOR_URL` bug** flagged
   above into a separate issue rather than smuggling it into #1759. It
   would have been caught by `validate_deployment_manifests`, which is
   evidence *for* the recommended approach but not evidence for bundling
   the fix.

Option A is recommended because the issue's core argument is that tools and
skills together close the validation loop that tools alone did not. Shipping
them together lets the acceptance criterion be something testable end-to-end
("re-run the #1692 validation pass using only the new tools/skills; does
any step still require raw `kubectl`?"). Options B/C/D either forfeit that
acceptance criterion or defer it behind more sequencing than the story warrants.

The patterns to reuse are boringly clear: schemas in `PIPELINE_TOOLS`,
handlers on `PipelineToolHandler`, orchestrator routes under
`orchestrator/routes/` for new HTTP surfaces, k8s work via the existing
`get_kubernetes_client()` singleton, skills under `skills/` alongside
the existing three. No new architecture.

## Open Questions

All open questions have been registered via `egg-contract` and are
reproduced here for reviewer visibility. Recommended options are marked
in each decision; the human gate at the end of refine decides which to
take.

### HITL Decisions

<!-- egg-hitl-decision id=decision-1 -->

**How should the proposed MCP tools and skills be decomposed into deliverables for #1759?**

- [ ] Keep #1759 as a single umbrella: one PR (or phase) that ships all five MCP tools and both skills together (Recommended)
- [ ] Split by shape: one PR for MCP tools, a second PR for skills once tools land
- [ ] Split per-item: open child issues for each tool and each skill and close #1759 as a tracker
- [ ] Land only the MCP tools now and defer both skills to a separate tracked issue
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-2 -->

**Where should the new operator-side skills (`deployment-diagnose`, `agent-diagnose`) live in the repo?**

- [ ] Add them under `skills/` alongside the existing `sdlc`, `babysit-pr`, `egg-setup` skills (Recommended — matches current convention; #1765's sandbox-side split can be introduced separately)
- [ ] Introduce a `skills/operator/` subtree now and move existing skills into `skills/agent/` so operator- vs agent-facing skills are visibly separated up front
- [ ] Keep under `skills/` but prefix the directory names (e.g. `skills/op-deployment-diagnose/`) to signal operator-only use without moving existing skills
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-3 -->

**Should the `submit_task` 'silently reuses an existing task_id' papercut be fixed as part of #1759, or deferred to a separate issue?**

- [ ] Fix within #1759 — add a `fresh=true` flag or reject the collision; small scope and tightly coupled to the validation pain the issue describes
- [ ] Defer to a separate issue — out of scope for an MCP-tooling/skills umbrella, file a follow-up instead (Recommended)
- [ ] Document-only within #1759: call out the non-idempotency in MCP tool docs and defer the behavior change to a later issue
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-4 -->

**`rebuild_and_rollout` is deferred in the issue pending #1763. Should we remove it from #1759's deliverables entirely?**

- [ ] Remove it from #1759's scope and wait for #1763 to land before opening a follow-up (Recommended — keeps this issue focused on gaps that are not blocked on other work)
- [ ] Keep a placeholder task in #1759 that no-ops until #1763 lands, so re-evaluation is guaranteed
- [ ] Ship a thin `rebuild_and_rollout` that calls `make redeploy` today (works locally) and assume #1763 unblocks CI/remote cases later
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-5 -->

**`agent-diagnose` needs logs from short-lived pods that are deleted before `kubectl logs` can fetch them. How should that log-persistence gap be addressed for #1759?**

- [ ] Defer the log-persistence fix to a separate issue; the `agent-diagnose` skill ships using only currently-available data (Recommended — keeps #1759 tractable and makes the persistence work independently reviewable)
- [ ] Block `agent-diagnose` on log persistence: include stream-to-orchestrator (or keep-last-failed-pod) work in #1759's scope
- [ ] Ship a minimal 'preserve failed pod for N minutes' flag as part of #1759 and leave full out-of-band persistence for later
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-6 -->

**`validate_network_isolation` spawns a throwaway probe pod in `egg-agents`. How should the probe image and runtime behavior work?**

- [ ] Reuse the sandbox image and run `curl`/DNS probes from a short-lived Job; delete the Job after results are collected (Recommended)
- [ ] Use a minimal dedicated probe image (e.g. `busybox` or `nicolaka/netshoot`) baked into the k8s manifests
- [ ] Don't spawn a live probe — statically infer isolation from NetworkPolicy manifests only, and document that runtime verification is out of scope
- [ ] Other (explain in reply)

---

<!-- egg-hitl-decision id=decision-7 -->

**MCP tools sit in the orchestrator's in-process MCP server, which runs inside `egg-system`. Some proposed tools (`validate_network_isolation`, `prune_stale_worktrees`) do real work that also needs gateway/filesystem state. Where should that logic live?**

- [ ] Orchestrator-owned: add new HTTP routes under `orchestrator/routes/` and have `PipelineToolHandler` proxy to them; cross-cluster calls go to gateway via existing clients (Recommended — matches the pattern used by all existing tools)
- [ ] Gateway-owned: add new gateway routes that the orchestrator's tool handler proxies to, since worktree state and NetworkPolicy config are closer to gateway concerns
- [ ] Split by tool: keep validation tools in the orchestrator but put worktree pruning in the gateway (closest to the worktree manager)
- [ ] Other (explain in reply)

---

### Open-Ended Feedback

The feedback request was registered via `egg-contract add-feedback` (feedback-1).
All questions reproduced below; the human gate reviews the comment and edits
it in place to respond.

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: What's the expected operator persona for the new operator-side skills — is it the egg developer running `make redeploy` locally on k3s, or a production operator running egg on a shared k3s/EKS cluster? That distinction affects output format, required vs. optional evidence, and whether the skills can assume kubectl context already resolves to `egg-system`.**

> _Your answer here_

**Q2: Are there any other identifier-translation asymmetries beyond #1760 and #1764 (Pod UID vs Job UID) that `agent-diagnose` should know about? The issue mentions this pattern is a 'reliable category' — flagging any known pairs (session ID ↔ container ID, issue number ↔ pipeline ID, task ID ↔ agent role, etc.) up front would make the pattern-matched error classifier comprehensive on day 1.**

> _Your answer here_

**Q3: Should the existing integration tests under `integration_tests/test_network_isolation.py` and `test_network_security.py` (which today probe Docker-era egress) be migrated to drive `validate_network_isolation` against the k8s backend, or should the MCP tool be a fresh implementation and the integration tests live on alongside it?**

> _Your answer here_

**Q4: `deployment-diagnose` proposes a 'silent-failure scan across the gateway logs' (referencing the 272-repeats #1767 pattern). What is the acceptable scanning window and log source — last N lines of the live gateway pod, all pod logs since the last rollout, or a persistent log aggregator that doesn't yet exist? A bounded scope here keeps the skill fast and lets us ship without waiting on a logging stack.**

> _Your answer here_

**Q5: The issue's 'silently reuses task_id' MCP papercut (at the end of 'Related MCP behavior worth documenting') — do you want this refine/analysis to recommend a specific fix shape (e.g., require `fresh=true` after cleanup) or just surface it as a known concern and leave the shape to the plan/implement phases?**

> _Your answer here_

**Q6: Are there deployment environments beyond k3s that #1759 should support on day one (EKS, GKE, OpenShift, kind)? Some of the proposed tools (`validate_deployment_manifests`, `get_deployment_context`) are portable but their warning rules (e.g. 'k3s containerd image presence') are k3s-specific — knowing the target matrix lets us pick which warnings to gate on runtime detection.**

> _Your answer here_

**Q7: `prune_stale_worktrees` will run against `~/.egg-worktrees/` which in k3s is mounted via a hostPath on the gateway node. Is it acceptable for this MCP tool to mutate host filesystem state (even under `dry_run=false`), or should destructive prune require an additional confirmation/flag beyond the existing `dry_run` switch?**

> _Your answer here_

**Q8: Is there an existing ops runbook or diagnostic playbook (internal wiki, docs page) that describes how the team currently debugs k3s deployment failures? Feeding those expected-evidence lists into `deployment-diagnose` and `agent-diagnose` as their initial corpus would save reverse-engineering from issue commentary.**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

## Complexity Assessment

**high**. Five MCP tools plus two skills touch orchestrator routes, the
in-process MCP server, the kubernetes client layer, kustomize manifests,
the worktree manager in the gateway, and three or four new docs pages. The
two skills alone compose six to eight distinct evidence-gathering primitives
and need pattern-matched error classification. This is not "single file,
known pattern"; it is a cross-cutting story with several independent
shippable pieces that the plan phase can reasonably parallelize across
agents.

---

*Authored-by: egg*
