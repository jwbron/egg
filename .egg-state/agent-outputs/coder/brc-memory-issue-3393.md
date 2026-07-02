# Coder BRC memory — issue-3393 (multi-repo pipelines)

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
