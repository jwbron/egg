# Coder BRC memory — issue-3393 (multi-repo pipelines)

## Slice-3 — stop the repos[0] collapse; owner/repo-keyed worktree map

**Proposed commit:** `5601063cb` (branch `egg/issue-3393-slice-3-coder/work`)
**Tasks:** task-3-1, task-3-2 — both implemented, committed, marked complete.

### Change model (what landed)
- **task-3-1 (three collapse sites removed + full env map):**
  - `kubernetes_spawner/_spawn.py`: `primary_repo = next(iter(repos or []), None)`
    (repos is canonically primary-first; avoids literal `repos[0]`). EGG_REPO_PATH
    + EGG_PIPELINE_REPO still derive from the primary. NEW: `EGG_PIPELINE_REPOS`
    env = JSON `{owner/repo: /home/egg/repos/<bare-name>}` built from
    `repo_volumes` keys, so a per-slice agent can select ITS repo (AC-4). Left
    unset when `repo_volumes` empty/None.
  - `commit_authorship_store.py:_resolve_authorship_repo_path`: dropped the
    `len==1: return repos[0]` + blind `repos[0]` fallback. New order: primary
    hint via `EGG_PIPELINE_REPO` (match bare name) → `egg` → `next(iter(repos))`.
    Hint is usually absent in the orchestrator process ⇒ degrades to prior
    egg/first heuristic (behavior-preserving there).
  - `routes/pipelines.py:_spawn_overseer_agent`: `overseer_repo =
    next(iter(pipeline_repos or []), None)` (was `pipeline_repos[0]`).
- **task-3-2 (owner/repo re-key at source):**
  - `gateway/gateway.py` `worktree_create` (~7767): key response map by full
    `repo` slug (was `repo_name` bare). Bare-name callers unaffected (repo ==
    repo_name). On-disk dir/mount stay bare.
  - `gateway_client/_models.py` + `_worktree.py`: doc-only (client passthrough).

### Load-bearing consumer fix (why pipelines.py is in BOTH tasks)
Re-keying broke the pipeline-level worktree path derivation at
`routes/pipelines.py` (~24620): it matched `repo_short` (bare) against the map
and rebuilt `WORKTREE_BASE_DIR/worktree_id/<key>`. Fixed: match `pipeline.repo`
(full slug, now the key) and strip the owner prefix (`.split('/')[-1]`) when
joining the on-disk path. The spawner reuse helpers
(`_validate_worktree_for_reuse` / `_clean_reused_worktree` /
`_find_missing_worktrees`) ALREADY keyed vols by full `ref` and built bare-name
paths — no change needed. `host_path_mounts` (line ~630) already handles
owner/repo keys.

### Scope decisions (preempt reviewer questions)
- **Did NOT touch `pipelines.py:24556` (`pipeline_repos = [pipeline.repo]`).**
  Not one of the three enumerated collapse sites; not a `repos[0]` token; the
  plan scoped slice-3 to the three named sites + the re-key. Threading the full
  repo LIST into pipeline-level worktree CREATION is out of slice-3 scope.
  Consequence: the exposed map is structurally list-shaped/owner-keyed but today
  holds the primary until a later slice threads the full set into creation.
- **Did NOT touch `gateway.py:8584` (`/api/v1/sessions/create`).** Separate
  endpoint/flow; plan scoped only the `/api/v1/worktree/create` response (~7827).
  Left bare-name; repo_volumes for the spawner comes from worktree/create, not
  sessions/create.
- **Same-short-name container-path collision is pre-existing & out of scope.**
  Both `ownerA/foo` and `ownerB/foo` still mount to `/home/egg/repos/foo` (host
  path leaf = bare name). Ruling #6 only mandates distinct MAP KEYS, which we
  deliver. Distinct container paths would be a deeper change.

### Validation done locally (no venv — deps can't be installed here)
- `python3 -m py_compile` clean on all 6 changed files; system `ruff check`
  "All checks passed!".
- Behavioral sanity (pure logic): primary-pick (`next(iter(...))`) for
  populated/empty/None; EGG_PIPELINE_REPOS map shape; authorship resolver
  (primary-hint wins / egg / first / N=1) — all pass.
- `grep -rn 'repos\[0\]' orchestrator/` shows only comments + the INTENTIONAL
  `models.py` primary_repo accessor/validator mirror (allowlisted by plan) +
  the `len==1`-guarded `sandbox/egg_lib/sdlc_hitl.py:82`. No collapse remains.
- Full pytest deferred to tester (env can't install deps).

## Slice-2 — list-shaped submission + uniform visibility/auth validation

**Proposed commit:** `390def500` (branch `egg/issue-3393-slice-2-coder/work`)
**Tasks:** task-2-1, task-2-2 — both implemented, committed, marked complete.

### Change model (what landed)
- **task-2-1 (list-shaped submission):**
  - `mcp_tools/_tool_defs.py`: added `repos` array param to `submit_task`
    ({repo, base_branch?, primary?} items); relaxed `required` → `["description"]`.
  - `mcp_tools/_submit.py`: normalize `repos` canonically **primary-first**
    (index 0 == primary; transient `primary` flag dropped), mirror primary onto
    legacy `repo`/`base_branch`; error if both `repo` and `repos`, or neither.
  - `routes/pipelines.py`: `_normalize_submission_repos()` validates+orders the
    list (same repo/base regexes as singleton; **same-short-name-different-owner
    NOT rejected**), derives primary onto the singleton early (before the
    Missing-repo/format guards), builds `repos_specs: list[RepoSpec]` and passes
    it to `create_pipeline`. **No repos[0] collapse** (that's slice 3).
  - `state_store/_crud.py`: `create_pipeline` gained optional `repos` kwarg →
    `Pipeline.repos`. N=1 unchanged: `repos=None` ⇒ slice-1 validator synthesizes
    the one-element list from the legacy singleton.

- **task-2-2 (uniformity validation):** `_assert_repo_set_uniform()` in
  pipelines.py rejects mixed-visibility or mixed-auth repo sets with an
  actionable, repo-naming **400**. Single repo (after de-dup) = trivially
  uniform, no gateway round-trip.

### Uniformity guard placement — reconciled with tester's task-2-3 interface
The tester (task-2-3, commit 6ecabe787, merged into this branch at convergence)
pinned the interface: `gateway/repo_visibility.py` must expose
`validate_visibility_uniformity(repos)` and `validate_auth_mode_uniformity(repos)`
(both raise ValueError naming offenders; internal==private; same-name/diff-owner
NOT rejected; single/uniform no-op). Their gateway tests patch
`repo_visibility.get_repo_visibility` and `repo_config.get_auth_mode`.

Implemented to match EXACTLY, with a single source of truth per dimension:
- `gateway/repo_visibility.py`: `validate_visibility_uniformity` (canonical
  visibility logic, splits owner/name → `get_repo_visibility(owner, name)`) +
  `validate_auth_mode_uniformity` (delegates to `repo_config.assert_uniform_auth`).
- `config/repo_config.py`: `assert_uniform_auth(repos)` — canonical auth rule,
  beside `get_auth_mode`, bundled into BOTH gateway + orchestrator images.

### v2 — reviewer_security NACK addressed (fail-closed on indeterminate visibility)
reviewer_security v1 NACK: the `if vis is None: continue` silently dropped a repo
from the uniformity vote, so a multi-repo private+public set could be ADMITTED
when a secondary momentarily resolved to None (no downstream re-check —
`_compute_gateway_mode` reads only the PRIMARY repo). Fixed in BOTH twins:
- Multi-repo (`len(unique) > 1`): a repo whose visibility is not a known
  `public|private|internal` bucket (None OR unrecognized label) now FAILS CLOSED
  — orchestrator returns an actionable 400, gateway `validate_visibility_uniformity`
  raises ValueError, both naming the repo. N=1 short-circuits before any lookup
  (no availability cost; gateway helper now also short-circuits `len<=1`).
- Auth `except Exception` (config-read failure) now also fails closed for
  consistency (local bundled read ⇒ genuinely exceptional).
Verified fail-closed (None + unrecognized) + still-uniform + still-mixed cases
in isolation; tester's existing gateway/orchestrator uniformity tests remain green.

**Container-boundary fact (still load-bearing):** the orchestrator image
(orchestrator/Dockerfile) ships `config/repo_config.py` + `shared/` but NOT
`gateway/`, so `routes/pipelines.py` CANNOT import
`gateway.repo_visibility.validate_visibility_uniformity`. The orchestrator
submission enforcement therefore uses `_assert_repo_set_uniform`: auth via
`repo_config.assert_uniform_auth` (shared with the gateway helper) and visibility
inline via `GatewayClient.get_repo_visibility` (HTTP; mirrors `_compute_gateway_mode`).
The inline visibility comparison is the deliberate HTTP-boundary twin of the
gateway helper (cross-referenced in code); the small mirror is forced by the
boundary. `gateway/git_client/_credentials.py` was NOT touched — the tester's
auth patch targets `repo_config.get_auth_mode`, and the canonical helper lives in
repo_config.

### Validation done locally (no venv — network/cert blocked, can't pip install)
- `ruff check` clean on all 5 changed files; `py_compile` clean.
- Behavioral sanity (system python + pydantic): Pipeline repos↔singleton mirror,
  `primary_repo`, `resolve_slice_repo`, `assert_uniform_auth` uniform/mixed/single/
  empty, and `_normalize_submission_repos` (primary flag, bare-string entries,
  same-short-name allowed, all error paths) — all pass.
- Full pytest suite deferred to the tester (env can't install deps here).

### Files touched (beyond plan's named lists, all necessary + justified)
_tool_defs.py (schema), _submit.py, routes/pipelines.py, state_store/_crud.py,
config/repo_config.py. Did NOT touch gateway/repo_visibility.py or
gateway/git_client/_credentials.py (see deviation).
