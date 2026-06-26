# Gateway Restricted-Path Rejection and Commit-Authorship Registry

The gateway enforces role-based file restrictions at push time. When an
agent push modifies a path the pushing role cannot write, the gateway
**rejects the push** with a structured `403 restricted_path_modified`
([#2039](https://github.com/jwbron/egg/issues/2039)) and points the agent
at the conditional-ACK recovery pattern
([#1998](https://github.com/jwbron/egg/issues/1998)). The
**commit-authorship registry** is the source of truth for per-commit
attribution and backs the rejection's own-authored-vs-pulled partition.

The current behavior is also summarized in the
[gateway README "File-Level Access Restrictions"](../../gateway/README.md#file-level-access-restrictions)
section.

## Problem

Role-based file restrictions ([#1494](https://github.com/jwbron/egg/issues/1494))
govern which files each role may push. The hard problem is **mixed-role
pushes**: once an agent pulls or merges in commits authored by another role
(cross-role handoff, rebase-on-main), a naive whole-diff file check sees
those peer-authored paths and blocks the entire push, trapping a
role-restricted producer whose branch inherited unrelated upstream commits.

The gateway therefore cannot reason about a push as a flat set of changed
files. It must know *who authored each commit* so it can check the pushing
role's write permissions against only that role's own-authored files and let
pulled cross-role commits through untouched.

## Why a commit-authorship registry?

Before the gateway can partition own-authored from pulled work, it needs to
know which role authored each commit. The session-level git identity set by
`sandbox/entrypoint.py` (`egg (coder) <coder@egg.local>`) is metadata only
and forgeable by a compromised sandbox. The gateway keeps a durable
`{sha → role}` mapping, written **authoritatively** from the session token
that created each commit.

### Observation point: gateway-inline, not sandbox hook

The registry observes commits **inline in `git-execute`**, not via a sandbox
git hook. A sandbox-installed hook is infeasible here:

- Sandbox containers have no direct `.git` access — the gateway shadows the
  worktree's `.git` via tmpfs (`sandbox/entrypoint.py`), so a hook installed
  there would never fire.
- The gateway sets `core.hooksPath=/dev/null` globally.
- `git commit --no-verify` would bypass any hook that did get installed.

The gateway is the sole commit-creation path via
`POST /api/v1/git/execute`. The handler snapshots `git rev-parse HEAD`
before and after each inner git invocation, then POSTs every new SHA to the
orchestrator's `/api/v1/commit-authorship/register` endpoint with the
session's role. This catches every commit-creating subcommand (`commit`,
`commit --amend`, `cherry-pick`, `revert`, `merge`, `rebase`) with no
sandbox-side code path and no hook to suppress.

### Registry semantics: first-wins

The registry is append-only and idempotent per SHA:

- The first `register(sha, role, pipeline_id, ...)` call is authoritative.
- Re-register with the **same role** is a no-op.
- Re-register with a **different role** is rejected with a `collision` audit
  event; the original binding is preserved. This prevents an
  observer-suppression-then-rewrite attack where a malicious agent swallows
  the initial observer call and later re-registers the same SHA under a
  different role.

### Storage

The registry is a sub-store on the `egg/pipeline-state` orphan branch
(`orchestrator/state_store.py`), sharded by pipeline ID
(`.egg-state/commit-authorship/<pipeline_id>.json`) with a `_orphan.json`
fallback for commits registered before a pipeline ID is known. It reuses the
existing `fcntl` + `RLock` + optimistic-versioning pattern — no net-new
durable store.

### HTTP surface

- `POST /api/v1/commit-authorship/register` — accepts
  `{sha, role, pipeline_id, repo, branch, patch_id}`. Role is authoritative
  from the session's inter-pod shared-secret; body-supplied values are
  logged but not trusted. `patch_id` (optional, `git patch-id --stable`) is
  recorded at registration time so attribution can survive a later SHA
  rewrite (rebase) via content-based lookup
  ([#2932](https://github.com/jwbron/egg/issues/2932)).
- `POST /api/v1/commit-authorship/lookup` — bulk lookup by SHA and/or
  patch-id: `{shas: [...], patch_ids: [...]} → {attribution: {sha: role | null}, patch_attribution: {patch_id: role | null}}`.
  At least one of `shas`/`patch_ids` is required. The push handler uses
  `shas` for normal attribution and `patch_ids` as a content-based fallback
  for commits whose SHA a rebase rewrote. An ambiguous patch-id (identical
  patch from two distinct roles) resolves to `null`.

Both endpoints require the gateway↔orchestrator shared-secret header.

## Push handler dispatch

`gateway/gateway.py::git_push` drives agent-role enforcement from per-commit
attribution:

1. **Attribute files**: `get_attributed_changed_files_in_push` walks the
   unpushed range via the per-commit `diff-tree` loop, captures the emitting
   SHA for each file, and does one bulk `lookup_bulk` against the registry to
   tag each `AttributedFile` with `authored_by: str | None`.
2. **Partition**: files with `authored_by == push_role` **or**
   `authored_by is None` (fail-closed) are treated as own-authored and
   subject to restrictions. Files with `authored_by == <known other role>`
   are pulled and exempt.
3. **Decide**:
   - No own-authored file blocked → plain push. The response adds
     `pulled_commits` if any commit in the range was cross-role.
   - Any own-authored file blocked → **reject** with
     `403 restricted_path_modified`. The response body carries `role`,
     `blocked_paths`, `recommended_action`, `doc_ref` (`#1998`),
     `pulled_commits`, and `attribution_fallback`. A category-specific
     `hint` (e.g. "Documentation changes belong to the documenter role.")
     is added when the blocked set matches a known category
     ([#2355](https://github.com/jwbron/egg/issues/2355)). No ref update,
     no remote push — the worktree is left exactly as the agent had it so it
     can drop the edits and re-propose.

The agent's sanctioned recovery is to drop the edits to the blocked paths
and re-propose with `--pre-merge-condition`, flagging a manual change for
the human reviewer per the conditional-ACK pattern
([#1998](https://github.com/jwbron/egg/issues/1998)).

### Attribution-fallback (fail-closed)

When `get_attributed_changed_files_in_push` returns an error or an empty
commit list (for example, a push with staged changes but no walkable commit
range), the handler enters **attribution-fallback** mode: every changed file
is treated as own-authored-and-unregistered. If any such file is blocked,
the push is rejected exactly as above with `attribution_fallback: true` on
both the audit event and the response. A blocked-file set can never reach
`git push` under agent credentials.

Audit events: `push_denied_restricted_path_modified` fires on every
rejection; `push_authorship_unregistered_fallback` fires whenever any commit
in the range had `authored_by=None`. Non-agent sessions (no
`g.session.agent_role`) skip attribution entirely and take the plain-push
path.

`EGG_AGENT_RESTRICTIONS_ENFORCE=false` is an emergency kill switch: the
filter short-circuits to warn-only plain push.

## Fail-closed invariant

Commits with no registry entry are treated as own-authored. The agent cannot
suppress the observer (it is gateway-inline and runs before the response is
returned), and any unregistered commit that reaches the push handler still
flows through the pushing role's restriction check. A file a role cannot
write cannot be pushed under that role's identity, even under an observer-gap
or attribution-unavailable scenario — in both cases the push is rejected
rather than partially applied.

Phase / anchor / protected-file / branch-ownership / private-mode /
concurrent-mode checks keep their own `403` behavior; the restricted-path
rejection is scoped narrowly to agent-role file restrictions.

## Deploy ordering

Ship the orchestrator image **first** so `/api/v1/commit-authorship/*` is
live before any gateway starts POSTing to it. The observer is best-effort
(logs a WARNING and continues on registry-unavailable), and push-time
unregistered commits fall through to fail-closed — both acceptable
transiently but avoidable by ordering.

No database migration. The state store creates the `commit-authorship/`
subdirectory on first write. Long-running sessions that predate a deploy
continue to work; their commits fall through to fail-closed at push time.

## Monitoring

- `push_denied_restricted_path_modified` — fires whenever a push is rejected
  because the diff modifies a path the role cannot write. Inspect the
  event's `attribution_fallback` boolean: `false` is the normal blocked case
  (registry attribution was available); `true` means the handler could not
  compute a commit walk and fell back to the attribution-unavailable
  rejection. A sustained spike on the `true` branch is worth investigating
  (mocked tests leaking into production, or `git rev-list` disagreeing with
  the handler's view of the unpushed range).
- `push_authorship_unregistered_fallback` — a steady trickle is normal
  (long-running sessions that predate a deploy). A **sustained spike**
  suggests the git-execute observer is missing some commit-creating
  subcommand; investigate which subcommand is being missed.

## Deployment prerequisites

The gateway's commit-authorship client calls the orchestrator's
`/api/v1/commit-authorship/{register,lookup}` routes, which live behind
`require_lifecycle_secret`. The gateway pod therefore needs
`EGG_LIFECYCLE_SECRET` injected the same way `orchestrator-deployment.yaml`
does — mounted from `gateway-secrets.lifecycle-secret`. Without the secret,
every register and lookup 401s and the whole feature degrades to fail-closed
own-authored on every push. This env var is set in
`k8s/base/gateway-deployment.yaml`; local dev picks it up from `.env`.

## Related documents

- [Git Isolation Architecture](git-isolation.md) — parent document covering
  the gateway's policy-enforcement model.
- [Agent Development Guide: Push Enforcement](../guides/agent-development.md#push-enforcement-and-cross-role-pushes) —
  operational view for agent authors.
- [`sandbox/agent-config/rules/push-recovery.md`](../../sandbox/agent-config/rules/push-recovery.md) —
  runtime rule surfaced to sandboxed agents.
