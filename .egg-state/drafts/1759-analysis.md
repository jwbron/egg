# Analysis: MCP tooling gaps for the new Kubernetes deployment

> Issue: #1759 | Phase: refine

## Problem Statement

PR #1692 migrated the egg control plane from Docker Compose to k3s. Validating
that migration on a fresh machine exposed a long tail of deployment bugs
(bad hostPath mounts, missing Secret references, health-endpoint path
mismatches, `ORCHESTRATOR_PORT` env collisions, `imagePullPolicy: IfNotPresent`
against absent tags, a silent pipeline FAILED caused by k8s monitor
false-positives, and orphaned worktrees from a Compose-era state dir) that
the current MCP tool surface could not help diagnose. Every debug loop fell
back to `kubectl get pods`, `kubectl describe pod`, `kubectl logs deploy/…`,
`docker build … && sudo k3s ctr images import -`, and `kubectl rollout
restart …`.

The MCP surface today is pipeline-centric — it assumes orchestrator+gateway
are already up and talking to each other, which is exactly the state that
breaks first on a k8s-native deployment. The issue proposes closing this gap
along two axes:

1. **New MCP tools** for deterministic, structured actions/fetches that
   agents and humans both benefit from (validation, isolation probes,
   worktree cleanup, deployment context).
2. **New skills** for diagnostic workflows where the value is judgment —
   knowing which evidence to collect, how to triage it, and how to narrate
   findings. Skills compose the underlying MCP tools.

The desired outcome is that the next end-to-end k3s validation pass — and
every routine diagnose-and-redeploy loop after it — can be driven entirely
from egg's MCP interface without dropping to raw `kubectl`, except for the
long-tail cases explicitly scoped out.

## Current Behavior

### MCP surface

MCP tools live in `orchestrator/mcp_tools.py` (≈1973 lines) and are served
by `orchestrator/mcp_server.py` via FastMCP over HTTP. Handlers are dispatched
from `PipelineToolHandler.handle_tool_call` (mcp_tools.py:686–730). The
current tool catalogue is organised into two domains:

- **Orchestrator-backed**: `submit_task`, `babysit_pr`, `get_status`,
  `cancel_task`, `list_tasks`, `provide_input`, `check_health`,
  `list_containers`, `get_container_logs`, `send_message`,
  `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`,
  `validate_config`, `restart_agent`, `restart_phase`, `advance_phase`,
  `start_phase`, `complete_phase`, `populate_contract`.
- **Gateway-backed**: `list_checkpoints`, `search_checkpoints`,
  `get_contract`.

None of these currently speak to Kubernetes directly. `list_containers` and
`get_container_logs` eventually call into `KubernetesSpawner` /
`KubernetesClient`, but they are scoped to *pipeline* containers and do not
expose cluster-level state (pod Events, control-plane image tags,
NetworkPolicy enforcement, manifest drift).

### Kubernetes client layer

`orchestrator/kubernetes_client.py` (945 lines) uses the synchronous
`kubernetes` Python client and exposes:

- `create_container()` — builds a Job via `BatchV1Api.create_namespaced_job`.
- `get_pod_logs()` — `read_namespaced_pod_log(tail_lines, since_seconds)`.
- `get_pod_status()` — `read_namespaced_pod()`, maps Pod phase to
  `ContainerStatus`, inspects `container_statuses[0].state.waiting.reason`
  for `ErrImagePull`.
- `get_pod_for_job()`, `list_containers()`, `wait_for_container()`,
  `is_connected()`.

What is **not** present today:

- Any use of the Events API (`CoreV1Api.list_namespaced_event`). Pod and Job
  events (scheduling failures, ImagePullBackOff reasons, NetworkPolicy
  denials from CNI) are invisible to the orchestrator.
- Any dry-run / validation path (`kubectl apply --dry-run=server`,
  `kube-score`, `kubeconform`, CEL rules). `grep -rn dry-run` hits only
  git flags.
- Any async k8s client usage.

### Kubernetes deployment

The k3s deployment is a kustomize base + overlay (`k8s/base`, overlays under
`k8s/overlays/local`). Salient facts for this analysis:

- Health endpoints standardised on `/api/v1/health` (gateway port 9851,
  orchestrator on its `api` port) — see
  `k8s/base/gateway-deployment.yaml:74,82` and
  `k8s/base/orchestrator-deployment.yaml:81,89`. The issue's `/healthz` vs
  `/api/v1/health` complaint is one the `validate_deployment_manifests`
  tool would have caught on a fresh overlay.
- Secrets are created imperatively (`make k3s-secrets`) into a
  `gateway-secrets` Secret in `egg-system`; manifests reference that name
  but nothing verifies it exists before apply.
- Six Calico NetworkPolicies in `k8s/base/network-policies.yaml` encode
  the fail-closed agent isolation story (`default-deny-ingress/egress`,
  plus four allow-lists: agent→gateway, agent→orchestrator,
  orchestrator→agent, agent→kube-dns). Nothing currently probes these
  policies at runtime.

### Agent spawner & session model

`orchestrator/kubernetes_spawner.py` (1082 lines) creates per-pipeline Jobs
in `egg-agents`, wires per-agent worktrees, and registers token-only gateway
sessions (IP-bound sessions don't work because Pod IPs are ephemeral).
Host-path volumes come from `EGG_HOST_REPO_MAP` and `EGG_HOST_WORKTREES_PATH`
env vars (currently hardcoded to `/home/jwies/...` in the local overlay —
flagged in #1760 as a portability follow-up).

### Worktree lifecycle

Two worktree locations matter:

- **Shared pipeline worktree dir**: `/home/egg/.egg-worktrees/<container_id>/
  <repo_name>` (`WORKTREE_BASE_DIR` in `gateway/worktree_manager.py:47`).
  Orphaned when a container is destroyed without running
  `remove_worktree()`.
- **Per-repo admin dirs**: `/home/egg/repos/<repo>/.git/worktrees/`. Stale
  when a worktree dir is removed by something other than `git worktree
  remove` (e.g. the Compose-era state dir the fresh k3s deploy inherited).

`WorktreeManager.prune_stale_worktrees()`
(`gateway/worktree_manager.py:1270-1366`) already runs `git worktree prune`
per repo, respects locks, and is invoked on gateway startup by
`startup_cleanup()`. `cleanup_orphaned_worktrees()`
(worktree_manager.py:1182) handles the shared worktree base dir with a
manual `shutil.rmtree` fallback.

So the plumbing for both cleanup paths exists — what is missing is an
**on-demand MCP tool** to run them outside of gateway startup, with a
`dry_run` mode that reports findings without mutating state.

### `submit_task` / `cancel_task` reuse semantics

- `submit_task` (`orchestrator/mcp_tools.py:753–862`) derives `pipeline_id`
  from `issue_number` (or `jira_ticket`). If a pipeline with that id exists,
  the orchestrator route returns HTTP 409 with `existing_pipeline_id` /
  `existing_status` / `existing_phase`. The MCP handler forwards these
  details back to the caller — it does **not** silently reuse state.
- `cancel_task(cleanup=True)` (mcp_tools.py:1328–1380) sends `PATCH
  status=cancelled` synchronously, then fires `DELETE /pipelines/{id}` in a
  daemon thread. The DELETE endpoint is documented as cleaning up
  "containers, remote branches, Redis messages, and the state file" — but
  worktrees are listed in the `cleanup=True` argument description, so the
  contract is ambiguous in code vs. docstring.

The issue's observation ("pulled pods from the cancelled run's state
(pre-fix image)") is therefore more likely either (a) a race between the
background DELETE and the immediate next `submit_task`, or (b) a stale
`:latest` image cache in k3s containerd that survived cleanup. #1763 fixes
(b). (a) is a separate race worth flagging but sits somewhat outside the
issue's scoped asks.

### Skills surface

`skills/` contains three skills today: `babysit-pr`, `egg-setup`, `sdlc`.
Each is a single directory with a `SKILL.md` file that has YAML front-matter
(`name`, `description`, `disable-model-invocation`, `argument-hint`) and a
markdown body of phased instructions. Skills invoke MCP tools through the
normal Claude tool-calling mechanism (not through hard-wired bindings).
There is no registry, no schema enforcement on SKILL.md beyond what the
harness parses, and no skill-level test harness — behavior is tested by
exercising the underlying MCP tools. There are currently **no** k8s or
diagnosis skills.

### Related work in flight

- **#1760** — k8s runtime reconciliation marks healthy agent pods FAILED
  during the `ContainerCreating → Running` transition. `kubernetes_monitor`
  needs to key off
  `containerStatuses[0].state.terminated.finishedAt`, not pod phase. This
  is the kind of failure the proposed `agent-diagnose` skill would make
  obvious (show pod state vs. orchestrator-asserted state side-by-side).
- **#1763** — images pinned to `:latest` mean `kubectl apply` sees no
  spec change after a rebuild; stale pod keeps running. Once SHA-tagged
  images land, the image-tag bullet in `get_deployment_context` becomes
  cheap and `rebuild_and_rollout` collapses to `make redeploy` — so the
  issue explicitly *defers* that tool.

## Constraints

### Technical

- **k8s API auth**: the orchestrator runs with the `egg-orchestrator`
  ServiceAccount + RBAC (`k8s/base/rbac.yaml`). Any new tools that query the
  cluster (events, manifest dry-run, network probe Job creation) may
  require RBAC additions (`events.list`, possibly `pods/exec` for probes).
  The issue implicitly asks for this without calling it out.
- **`kubernetes_client` is synchronous**. New tools can follow that
  convention; introducing async would require a broader refactor.
- **MCP output size cap and stiff schema**. The issue itself splits on this
  axis: structured, bounded output → tool; "dump me everything useful" →
  skill. We should not try to make `deployment-diagnose` a single MCP tool.
- **Calico vs. Flannel**: NetworkPolicies only bite on Calico (`make
  k3s-setup` forces `--flannel-backend=none --disable-network-policy`).
  `validate_network_isolation` must warn, not fail, when running on a
  cluster without NetworkPolicy enforcement.
- **Short-lived agent pods**: agent pods that crash in <10s are deleted
  before `kubectl logs` can fetch anything. The issue flags this as a
  **separate concern** but makes `agent-diagnose` "only as useful as the
  log data it can still reach." Any diagnose skill that depends on post-hoc
  log retrieval will be unreliable until log capture is addressed (enable
  `restartPolicy: OnFailure` with `--previous`, project logs to gateway,
  or set `ttlSecondsAfterFinished` high enough for a follow-up fetch).
- **Tool outputs should be safe by default**: `prune_stale_worktrees` is
  the only mutating tool proposed. Default should be `dry_run=true`.
- **Context discovery**: `get_deployment_context` needs to discriminate
  Docker vs. Kubernetes runtime. `KubernetesClient.is_connected()` is the
  obvious probe; orchestrator already knows which spawner it loaded.
- **Validation lint choice**: `kubeconform` (schema only, fast, no
  cluster) vs. `kubectl apply --dry-run=server` (full admission, needs
  cluster) vs. hand-rolled Python checks. The issue's checklist is
  semantic ("Secret exists", "hostPath exists", "Service selector matches
  pod labels") which `kubeconform` alone will **not** catch — semantic
  checks belong in the tool, structural checks can be delegated.

### Business / scope

- **Non-goals (explicit in issue)**: no full k8s operator; no replacement
  for `kubectl`; read-mostly with a narrow write action
  (`prune_stale_worktrees`). Anything more destructive stays explicit.
- **`rebuild_and_rollout` is deferred** pending #1763 land.
- **Scope sprawl risk**: the issue proposes four tools, two skills, and
  flags two separate concerns (submit_task reuse docs, short-lived pod
  logs). Shipping in one PR would be very large; staging the work is a
  **plan**-phase decision, not a refine-phase one, so I am not carving
  phases here — but I am surfacing scope as an open question.

### Dependencies

- **#1763 (image SHA tagging)**: `get_deployment_context` is more useful
  once images carry SHA tags; `rebuild_and_rollout` is only worth
  considering after it lands.
- **#1760 (monitor false-positive fix)**: `agent-diagnose` is most
  useful after the monitor stops pre-emptively marking pods FAILED,
  but it can ship before — in fact it would have surfaced #1760 faster.
- **Short-lived pod log capture** (flagged, not tracked): blocks
  `agent-diagnose` from being reliable in its worst case.

## Options Considered

### Option A: Build everything the issue asks for, in one feature set

**Approach**: Deliver all four tools (`validate_deployment_manifests`,
`validate_network_isolation`, `prune_stale_worktrees`,
`get_deployment_context`) plus both skills (`deployment-diagnose`,
`agent-diagnose`), and also patch the `submit_task` reuse behaviour to
require an explicit flag. Defer only `rebuild_and_rollout` per the
issue's own recommendation.

**Pros**:
- Matches the issue's framing exactly.
- Each item was motivated by a real debugging session, so coverage is
  already validated.
- Shipping the skills alongside the tools they compose means agents see
  the full diagnosis workflow on day one.

**Cons**:
- Large scope for a single refine→plan→implement cycle. Reviewer
  cost is high; regressions are harder to isolate.
- Mixes tool work, skill work, manifest/RBAC work, and orchestrator-route
  work — touches four subsystems.
- Ties `agent-diagnose` reliability to a fix for short-lived pod log
  capture that is not in scope.
- Ties `get_deployment_context` image-tag UX to #1763 landing first.

### Option B: Sequence by dependency, land tools first, then skills

**Approach**: Land the four tools (and the `submit_task` doc/flag
clarification) as the first unit of work. Skills come after, once the tools
they compose are stable. Keep `rebuild_and_rollout` explicitly deferred.
Separately, acknowledge in this analysis that short-lived pod log capture
is a prerequisite for `agent-diagnose` reliability and tee it up as
follow-up work (don't try to solve it here).

**Pros**:
- Tools are independent units — each can be reviewed and tested on its
  own. MCP output schemas are the right forcing function for clean APIs.
- Skills become straightforward to write once the primitives exist (the
  issue itself notes "most of which are existing Kubernetes API calls,
  some of which are the MCP tools above"). Skill work becomes a
  documentation+orchestration task rather than a plumbing task.
- Decouples the pod-log-capture dependency from shippable wins.
- Natural first milestone: "the next `make deploy` validation pass can
  be driven from MCP".

**Cons**:
- Skills ship later, so initial value is narrower.
- Two reviewable artefacts instead of one.
- Risk of the skill phase sliding if other priorities intervene.

### Option C: Skills-first, thin tools

**Approach**: Ship `deployment-diagnose` and `agent-diagnose` as skills that
shell out to `kubectl`/existing MCP tools today, adding new MCP tools only
where a raw-`kubectl` approach won't work (the network-isolation probe, the
worktree pruner). Validation of manifests gets pushed into CI or a Makefile
target rather than an MCP tool.

**Pros**:
- Fastest to first diagnosis-user-value.
- Minimises MCP schema surface area.
- Matches the issue's warning that "dump everything" belongs in skill
  shape.

**Cons**:
- Skills that shell out to `kubectl` are harder to run from agent pods
  (by design, agents don't have `kubectl` inside the sandbox). The tools
  the issue proposes exist specifically so agent-side auto-recovery
  becomes possible — dropping that reduces the issue to a human-only
  debug aid.
- Punts `validate_deployment_manifests` to a different lifecycle
  (CI/Makefile), which doesn't help the "agent-initiated redeploy"
  path the issue anticipates.
- Doesn't address the agent-accessibility goal that motivated the
  tool/skill split in the first place.

### Option D: Minimal subset — only what the last validation session
required, plus doc fixes

**Approach**: Ship `prune_stale_worktrees` (worktree orphans broke the
first submission), `validate_deployment_manifests` (catches most of the
static bugs from the session), and add documentation to `submit_task` /
`cancel_task` about the reuse race. Everything else gets separate issues.

**Pros**:
- Smallest possible surface. Low risk.
- Directly maps to the "saved hours in that session" subset the issue
  calls out.

**Cons**:
- Leaves `validate_network_isolation` unshipped, so the Calico
  NetworkPolicy posture remains unverified at runtime — which is one of
  the PR's least-tested claims.
- No diagnose skills, so every future k8s bug will still fall back to
  `kubectl describe`.
- Fragments the issue into follow-ups for unclear reason — all four
  tools have concrete, validated use cases.

## Recommended Approach

**Option B (sequenced: tools → skills, `rebuild_and_rollout` deferred).**

Rationale:

1. The tool-vs-skill split in the issue is a *good* split and should be
   honoured: the tools become the primitives the skills compose.
   Landing them first gives the skills a stable foundation and gives
   `make deploy` validation a bounded, reviewable win.
2. Tools are independent of each other and can be planned as parallelizable
   work in the plan phase. Skills cannot start until the tools they
   compose exist, so ordering is forced by the call graph.
3. #1763 and #1760 are in flight. Deferring `rebuild_and_rollout` (per the
   issue) and letting #1760 ship first for `agent-diagnose`'s usefulness
   are concrete sequencing hooks that make Option B cleaner than Option A.
4. The pod-log-capture concern is **flagged** but kept out of scope —
   the analysis treats it as a prerequisite for `agent-diagnose`
   reliability rather than part of this issue's deliverables. That
   should be a separate issue.
5. The `submit_task` reuse behaviour is a doc / flag question with low
   implementation cost but unclear desired behaviour (add `fresh=true`?
   make cleanup synchronous? just document?). It should ride with the
   tools phase but be sized explicitly in plan.

Key decisions the plan phase needs to make but this analysis does not:

- Where exactly each new tool lives in `mcp_tools.py` (orchestrator-backed
  vs. a new `k8s-backed` domain section).
- Whether validation uses `kubeconform` (external binary, schema-level)
  or hand-rolled Python on parsed YAML (more work, but catches all the
  semantic bugs the issue lists — Secret existence, hostPath existence,
  Service/Pod label alignment, env collisions).
- Exact RBAC additions to `k8s/base/rbac.yaml` for events + probe-Job
  spawning.
- Whether `validate_network_isolation` uses a throwaway Job (keep model
  simple, costs a Job lifecycle) or `pods/exec` into an existing agent
  pod (cheaper, but narrows the probe to the agent's exact network
  identity).

## Open Questions

> Registration note: `egg-contract add-decision` is currently blocked for the
> `refiner` role in this deployment — the gateway's `get_role_from_context`
> (`gateway/contract_api.py:145-190`) only accepts the four values in the
> `Role` StrEnum (`implementer`, `reviewer`, `human`, `system`), so
> `Role("refiner")` returns `None` and the contract-mutate call 400s with
> "Cannot determine agent role." The practical workaround that matches
> the HITL processor's expectations is inline `<!-- egg-hitl-decision -->`
> and `<!-- egg-hitl-feedback -->` markers (see `1028-analysis.md:259-291`
> for the exact format), which is what this analysis uses. In parallel,
> every decision below has also been registered via
> `egg-orch decision create` so the pipeline's decision queue knows
> about them; the role-mapping fix should be raised as a follow-up issue
> distinct from #1759.

### Decisions

<!-- egg-hitl-decision id=decision-1 -->
**1. How should we slice the issue into shippable units?**
- **Option A: One feature set** — all four tools + both skills + `submit_task` fix in a single plan→implement cycle (matches issue framing; large scope)
- **Option B: Sequenced** — tools land first, then skills; `rebuild_and_rollout` deferred per issue (Recommended)
- **Option C: Skills-first** — shell out to `kubectl`/existing tools, add MCP tools only where `kubectl` won't work
- **Option D: Minimal subset** — only `prune_stale_worktrees` and `validate_deployment_manifests` plus a `submit_task` doc fix; everything else becomes follow-ups

<!-- egg-hitl-decision id=decision-2 -->
**2. What should back `validate_deployment_manifests`?**
- **External `kubeconform` binary** for schema + Python for semantic checks (mixed runtime deps)
- **`kubectl apply --dry-run=server`** for structure + Python for semantic checks (requires live cluster access at validation time)
- **Pure Python on parsed kustomize output** — no external deps, most portable, more code to maintain (Recommended)
- **Delegate structural to `kubectl --dry-run=client`**, do all semantic checks in Python

<!-- egg-hitl-decision id=decision-3 -->
**3. How should `validate_network_isolation` run its probes?**
- **Spawn a throwaway probe Job in `egg-agents`** and read its logs — isolated, costs a Job lifecycle per call (Recommended)
- **`pods/exec` into an existing agent pod** if one is present, else spawn a Job — cheaper when pipelines are live, wider RBAC
- **Always use `pods/exec` into a long-lived probe Deployment** added to the overlay — persistent overhead, fastest per call

<!-- egg-hitl-decision id=decision-4 -->
**4. What should happen when `submit_task` is called with a `pipeline_id` whose previous run was just cancelled with `cleanup=true`?**
- **Add a `fresh=true` flag** on `submit_task`; without it, keep the current 409 behavior; with it, wait for in-flight cleanup then create (Recommended)
- **Make `cancel_task(cleanup=true)` block** until cleanup finishes (synchronous); no `submit_task` change
- **Document only** — `cancel_task(cleanup=true)` + immediate `submit_task` is documented as non-idempotent; caller waits
- **Have `submit_task` detect** a cancelled+cleanup-in-progress state and queue the new run behind it

<!-- egg-hitl-decision id=decision-5 -->
**5. Short-lived agent pods (<10 s lifespan) lose their logs before `kubectl logs` can fetch them. Where should fixing that land?**
- **Separate follow-up issue**; `agent-diagnose` ships with documented best-effort log fetch (Recommended)
- **Pull it into #1759's scope** — needed for `agent-diagnose` to be reliable
- **Fold it into #1760** (the reconciliation false-positive is the main producer of short-lived pods)

<!-- egg-hitl-decision id=decision-6 -->
**6. What should the default be for `prune_stale_worktrees`?**
- **`dry_run=true` by default**; caller must pass `dry_run=false` to actually remove — matches issue text (Recommended)
- **`dry_run=false` by default** with a preview section in the response; matches `cancel_task(cleanup=true)` ergonomics
- **Two separate tools** — `list_stale_worktrees` (read-only) and `prune_stale_worktrees` (write)

<!-- egg-hitl-decision id=decision-7 -->
**7. `get_deployment_context` exposes the image tag currently running per component. Until #1763 lands (SHA-tagged images), the tag is `:latest` which is ambiguous. What should the tool do?**
- **Ship now; return `:latest` with a warning**; stop warning once SHA tags land (Recommended)
- **Block this tool on #1763**
- **Ship now and also surface the image digest** (`imageID`) which is unique even for `:latest`

### Feedback (Open-Ended)

<!-- egg-hitl-feedback id=feedback-1 -->
**8. Additional validation checks for `validate_deployment_manifests`**: Beyond the six the issue lists (Secret/ConfigMap exists, hostPath exists, `imagePullPolicy: IfNotPresent` tag-present, Service selector matches pod template, Service-env-var collisions with `GATEWAY_PORT`/`ORCHESTRATOR_PORT`), are there project-specific anti-patterns you've seen recur that belong in this tool? e.g., missing `runAsNonRoot`, missing resource limits, `hostNetwork: true`, `capabilities.add: [SYS_ADMIN]`, Pod mounts not matching the `fsGroup`, etc.

<!-- egg-hitl-feedback id=feedback-2 -->
**9. RBAC posture for the orchestrator ServiceAccount**: `events.list` is the obvious addition. Beyond that, do you want `pods/exec` gated behind a separate ServiceAccount, restricted by Role to specific namespaces only (e.g., only `egg-agents`), or run the probe Job under its own SA? What's the security bar you want to hold the new tools to?

<!-- egg-hitl-feedback id=feedback-3 -->
**10. `validate_network_isolation` pass/fail semantics**: The issue lists four claims the probe should verify. Should failure of any single claim fail the whole tool call (machine-friendly for auto-recovery agents), or should we always return structured per-claim results (`{claim: "no internet", pass: true/false, evidence: ...}`) and let the caller decide? The current MCP tool conventions lean toward structured results.

<!-- egg-hitl-feedback id=feedback-4 -->
**11. `deployment-diagnose` evidence ordering**: The issue says "lead with whichever piece of evidence made the failure obvious, not dump everything." Do you have a preferred ranking heuristic — e.g., always start with Events → container statuses → image drift → envs → health-body — or should the skill infer ordering from the symptom (`ErrImagePull` → lead with image tag; `CrashLoopBackOff` → lead with last-exit reason + logs; `Pending` → lead with scheduling Events)?

<!-- egg-hitl-feedback id=feedback-5 -->
**12. `get_deployment_context` cross-runtime portability**: Should the tool degrade cleanly across runtimes (return `runtime: docker` with k8s fields omitted when run against a Compose deployment), or hard-fail and tell the caller to use a Compose-specific path? Issue says the platform now targets k8s, but Docker dev loops may still exist.

<!-- egg-hitl-feedback id=feedback-6 -->
**13. Tool naming convention**: The issue uses `validate_deployment_manifests`, `validate_network_isolation`, `prune_stale_worktrees`, `get_deployment_context`. Alternatives: `k8s_validate_manifests`, `k8s_probe_isolation`, `gateway_prune_worktrees`, `k8s_get_context`. Domain prefixes make intent obvious in the MCP tool list; unprefixed reads better. Preference?

## Complexity Assessment

**high**

Rationale:

- Four new MCP tools, each touching a distinct subsystem (manifest parsing;
  k8s Events + probe Jobs; gateway WorktreeManager; k8s client + runtime
  discovery).
- Two new skills that compose the tools and require SKILL.md design and
  ranking heuristics.
- A doc/flag change on `submit_task` / `cancel_task` reuse semantics.
- Cross-cutting concerns: RBAC updates to `k8s/base/rbac.yaml`, possible
  new NetworkPolicy entries for a probe Job, and a latent prerequisite
  (short-lived pod log capture) that affects skill reliability.
- Multiple independent phases exist (tool set A independent of tool set B
  independent of each skill), which is the textbook profile for
  high-complexity, parallelizable work in the plan phase.

---

*Authored-by: egg*

# metadata
complexity_tier: high
parallel_phases: true
