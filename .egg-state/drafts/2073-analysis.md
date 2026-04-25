# Analysis: Simplify repository configuration: schema cleanup, repo+local config layering, onboard skill, validator

> Issue: #2073 | Phase: refine

## Problem Statement

Onboarding a new repo to egg means hand-authoring a per-repo block in
`~/.config/egg/repositories.yaml` — a single user-level YAML file whose schema
has several traps that surface only at runtime, weeks after the broken config
has been baked into a sandbox image:

- `persist_dirs` vs `persist_system_dirs` is the difference between "binary
  survives into the runtime sandbox" and "binary is silently dropped between
  Docker stages." #2065 was exactly this trap.
- `watch_files` must enumerate every dependency manifest the `build_commands`
  touch. Miss one and Docker layer caching silently serves stale dep installs.
- `build_commands` run as root in a synthetic working directory under
  `/tmp/repo-deps/<repo>` containing only the watch files — a different
  shape than the developer's local install flow. #2087 caught a `uv sync`
  command that worked locally but failed under the watch-files-only context.
- `checks` entries reference Makefile targets, but no validator confirms those
  targets exist; a typo in `make tset` is only caught at runtime, mid-pipeline.
- The config lives **only** on the operator's machine. Every onboarder
  rediscovers the same per-repo block from scratch even though most of the
  answers (build commands, lockfiles, persist paths) are deterministic
  properties of the repo itself.

The desired outcome is to cut the onboarding error surface to near zero
through four tightly related changes: a simpler schema, a layered
repo-defaults + user-overrides config model, an onboarding skill that
introspects a repo and emits a correct block, and a validator that catches
the known footguns at config-write time (not image-build time, and not weeks
later when an agent quietly falls back to a system Python).

## Current Behavior

Configuration today is a single user-level file:

- **One file:** `~/.config/egg/repositories.yaml` (location resolved by
  `config/repo_config.py::_get_config_path()` with three fallbacks:
  `EGG_REPO_CONFIG` env var → `~/.config/egg/repositories.yaml` → in-container
  mount at `~/repos/egg/config/repositories.yaml`).
- **Schema:** documented in `config/repositories.yaml.example` (245 lines).
  Top-level keys mix operator-scoped fields (`github_username`,
  `bot_username`, `writable_repos`, `local_repos`, `default_reviewer`,
  `user_mode`, `github_sync`, `docker_setup`) with a `repo_settings:` map
  whose values are the per-repo blocks (`build_commands`, `persist_dirs`,
  `persist_system_dirs`, `watch_files`, `checks`, `auth_mode`,
  `checkpoint_repo`, etc.).
- **Loader:** `config/repo_config.py` (724 lines) provides `get_*` helpers.
  No central schema validation — each helper does its own type-coercion.
  `validate_checks` (in `shared/egg_config/validators.py`) is the only
  schema-level validator and it just filters malformed list entries.
- **Build flow:** the launcher (`sandbox/egg_lib/docker.py::_copy_repo_watch_files`)
  reads the user file, copies declared `watch_files` from each local repo
  into `<config-dir>/repo-deps/<owner>--<repo>/`, and writes a sibling
  `manifest.json` mirroring the build_commands block. During the multi-stage
  Docker build, `sandbox/docker-setup.py::run_build_commands` reads that
  manifest (since `repositories.yaml` is not in the build context) and
  executes the commands; `persist_build_dirs` then copies declared
  `persist_dirs` and `persist_system_dirs` to `/opt/prebuilt-deps/...`,
  and the entrypoint restores them at container start.
- **Validation today:** there is no `egg validate-config` for repo settings.
  `scripts/validate-config.py` validates **secret** configs (`SlackConfig`,
  `GitHubConfig`, `GatewayConfig`, `JiraConfig`, `ConfluenceConfig`) but
  does not parse or check `repositories.yaml`. The `validate_config` MCP
  tool in `orchestrator/mcp_tools.py:689` validates *pipeline* configs,
  not repo configs — name collision.
- **Onboarding today:** `skills/egg-setup/SKILL.md` covers the Phase 4
  repository configuration step, but it asks the user to hand-author check
  commands and does not detect lockfiles, propose `build_commands`, or
  generate `persist_dirs`. The result is that even users who run the
  guided setup still have to know the schema's traps.
- **Repo-side `.egg/`:** the repo root already contains `.egg/` with
  `contract-rules.md`, `phase-permissions.json`, and `schemas/` — used by
  the SDLC pipeline. Adding a `repositories.yaml` there is mechanically
  fine; the dir is the natural home for repo-side egg config but is
  currently scoped to pipeline schemas.
- **Recent footgun fixes:** #2090 made `docker-setup.py` raise (instead of
  warn-and-continue) on missing post-build paths and a new `make
  sandbox-deps` target wraps `uv sync --no-install-project` for the
  watch-files-only build context. That hardens the runtime, but the config
  itself is still hand-authored and unchecked at write time.

## Constraints

- **Backward compatibility.** Existing users have a working
  `~/.config/egg/repositories.yaml`. The current schema (`persist_dirs` +
  `persist_system_dirs`, explicit `watch_files`, explicit `checks`) must
  keep loading — the simplification is additive, not a breaking rename.
  How long the deprecation window runs is an open question.
- **In-container config availability.** During Docker builds the file is
  not in the build context — `sandbox/egg_lib/docker.py::_copy_repo_watch_files`
  writes a `manifest.json` snapshot that `docker-setup.py` reads. Any new
  schema (collapsed `persist`, layered repo+user config) has to be
  pre-merged on the host before the manifest is written; the build-time
  reader sees only the merged result.
- **Loader paths to update consistently.** `repositories.yaml` is parsed
  by ~15 callers across `config/repo_config.py`, `gateway/`, `orchestrator/`,
  `sandbox/egg_lib/`, `scripts/`, and `integration_tests/`. The merge layer
  has to land in one place (likely `config/repo_config.py::_load_config`)
  so every caller sees the same merged view without each one re-implementing
  the deep-merge.
- **Repo-defaults file checkin.** The new `<repo>/.egg/repositories.yaml`
  must contain only **per-repo-scoped** keys. Operator identities
  (`github_username`, `bot_username`, `writable_repos`, `local_repos`,
  `default_reviewer`, `user_mode`) are scoped to the operator and must
  never leak into a committed file — both as a leak hazard and as a
  correctness hazard if a different operator clones the repo.
- **List-merge surprise budget is low.** Deep-merging dict-of-dict is
  predictable; deep-merging lists is not. `checks`, `persist`, `watch_files`,
  `extra_packages.apt`, `extra_packages.dnf`, `local_repos.paths` are all
  list-valued. The proposal recommends "replace by default, opt-in extends"
  — sticking to that keeps the merge rules teachable.
- **Heuristic detection has to fail loudly when wrong.** The onboarding
  skill detects build_commands from lockfiles. If detection produces a
  wrong block silently (e.g. picks `npm install` when the repo is on
  pnpm), the user pays the same "looks right, builds, silently degrades"
  cost the issue is trying to eliminate. Any heuristic must surface what
  it detected and ask for confirmation.
- **Validator as net-new safety net, not the only safety net.** #2090
  already hardened the build-time check. The validator catches the same
  classes earlier in the lifecycle but must not be the only line of
  defense — fail-loud at build time stays in place.
- **Schema-versioning the repo file.** A repo committing
  `.egg/repositories.yaml` to source control couples its history to
  egg's schema churn. A `version: 1` field with graceful degradation on
  newer versions is cheap insurance.

## Options Considered

### Option A: Ship all four changes together as one feature

**Approach:** Land schema simplification, layered repo+local config, onboard
skill, and validator as a single coordinated change with shared tests and
docs.

**Pros:**
- The four pieces reinforce each other: the validator catches what the
  schema didn't simplify away; the skill writes what the validator approves;
  the layered model is what makes the skill's output useful (committing it
  to the repo).
- Single migration story for users — one set of release notes, one
  deprecation window.
- Tests can cover end-to-end onboarding flow.

**Cons:**
- Larger blast radius: a regression in any one piece blocks the others.
- Long PR cycle; harder to bisect if something breaks.
- The proposal's own "Out of scope" includes templates and migration
  tooling — pushing more into one PR risks creeping past that line.

### Option B: Sequence the four pieces as separate PRs

**Approach:** Land in dependency order:

1. **Schema simplification + validator** (no behavior change for existing
   users; new `persist:` accepted as alias; validator surfaces existing
   traps). Lands the deepest invariants first.
2. **Layered repo-defaults + local-overrides loader** (enables
   `<repo>/.egg/repositories.yaml`; merge logic centralized in one
   loader).
3. **Onboard skill** (consumes the simplified schema and the layered
   model — easy to write only after both exist).
4. **Migration helper** (`egg config migrate` flagged out-of-scope but
   trivially implementable once schema is settled).

**Pros:**
- Each PR is small, reviewable, and rollbackable.
- Schema + validator can ship without the layered model — early users get
  the "validate before bake" win immediately.
- The skill is the user-facing payoff; sequencing it last means it ships
  against a stable schema rather than chasing a moving target.

**Cons:**
- Documentation has to track the in-flight schema across two-or-three
  releases.
- Users who onboard between the schema-simplification and the
  skill-landing PRs still hand-author config — net win is delayed.
- More coordination overhead (each PR re-validates against the shared
  loader).

### Option C: Ship layered config + skill first; defer schema simplification

**Approach:** Keep the existing `persist_dirs` / `persist_system_dirs` /
`watch_files` schema. Land the layered repo+user model and the onboarding
skill so the per-repo block can ship in the repo. Defer the schema
collapse to a follow-up.

**Pros:**
- Less churn for existing config consumers (~15 callers don't have to
  learn a new schema).
- The skill's main payoff (correct block written by detection, not by
  hand) lands sooner.

**Cons:**
- The skill has to emit the **legacy** schema, which keeps the original
  traps (`persist_dirs` vs `persist_system_dirs`) baked in — operators
  reading the committed file still need the deep knowledge of
  `docker-setup.py` to make sense of it.
- The validator's most useful checks (the trap classes the schema
  collapse eliminates) become moot once the schema is collapsed; building
  them now is throwaway work.

### Option D: Repo defaults at `egg.yaml` (visible) instead of `.egg/repositories.yaml`

**Approach:** Same layered model, but commit the per-repo block at
`egg.yaml` (or `.egg.yaml`) at the repo root rather than under `.egg/`.

**Pros:**
- Discoverable: a new contributor sees the file in `ls`.
- Shorter path to type / mention in docs.

**Cons:**
- Adds a top-level file to every onboarded repo — visible clutter.
- The `.egg/` directory already exists in the egg repo and is the natural
  home for repo-side egg config (contract rules, phase permissions,
  schemas). Putting `repositories.yaml` next to those files is more
  consistent than scattering egg config across the root.
- `.egg/` gives room for future repo-scoped artifacts (per-repo
  templates, cached state, prebuilt-deps manifests) without re-litigating
  filename conventions.

### Option E: Loader change in one place vs. each consumer reimplements

**Approach (E1):** Centralize the merge in
`config/repo_config.py::_load_config` so every existing helper
(`get_repo_setting`, `get_repo_build_commands`, `get_repo_checks`, etc.)
sees the merged dict transparently. Sandbox/launcher code paths
(`sandbox/egg_lib/docker.py::_load_repos_config`) call the same helper or
mirror the merge logic via shared util.

**Approach (E2):** Have each consumer (gateway, sandbox launcher,
orchestrator) re-merge per-call.

**Pros (E1):**
- Single source of truth for merge rules. Bug fixes land in one place.
- Existing call sites see the merged config without changes.

**Cons (E1):**
- `config/repo_config.py` and `sandbox/egg_lib/config.py` currently
  read the user file independently. Centralizing means lifting the merge
  into a shared module both can import — likely
  `shared/egg_config/repos.py` (new) — and updating both readers.

E2 is strictly worse (every consumer redoes the work and merge bugs
diverge) — kept for completeness only.

## Recommended Approach

**Combine Option B (sequenced delivery) + Option D's filename choice
(`.egg/repositories.yaml`) + Option E1 (centralized merge).**

- **Sequenced delivery (B)** keeps each change reviewable. Schema
  simplification + validator first means the rest of the work lands on a
  stable, validated foundation; the skill's output is the simpler schema
  by the time it ships.
- **`.egg/repositories.yaml` (D's "Cons against")** matches the existing
  repo-side `.egg/` directory and leaves room for future per-repo egg
  artifacts. The visibility cost is real but small — the file is
  documented in onboarding and exists alongside other repo-side config.
- **Centralized merge (E1)** keeps the merge rules in one place. The
  shared util belongs in `shared/egg_config/` (new module) so
  `config/repo_config.py` and `sandbox/egg_lib/docker.py` import the same
  loader; `gateway/`, `orchestrator/`, and `scripts/` keep using their
  current entry points and inherit the merged view for free.

The four pieces together hit the proposal's stated goal — onboarding error
surface near zero — without requiring a single oversized PR. The
**plan phase** should decide PR granularity, sequencing, and which pieces
are nice-to-have vs. blocking.

## Open Questions

The questions below are **registered** as decisions and feedback items in
this contract — the human will see them in the issue thread and can answer
there. They are listed here for reviewer context.

### Multiple-choice decisions (registered via `egg-contract add-decision`)

- **decision-1** — Where should the repo-defaults config file live?
  (`.egg/repositories.yaml` recommended; alternatives: `egg.yaml`,
  `.egg.yaml`)
- **decision-2** — Ship all four changes together or sequence as separate
  PRs? (sequenced recommended)
- **decision-3** — How should the unified `persist:` list classify
  entries? (leading-slash classification recommended)
- **decision-4** — Deprecation policy for the legacy schema?
  (accept-both-with-warning recommended)
- **decision-5** — Where should the centralized merge loader live?
  (new `shared/egg_config/repos.py` recommended)
- **decision-6** — How should the validator be exposed? (CLI + MCP tool
  recommended; today's `validate_config` MCP name collision needs
  resolving)
- **decision-7** — How should onboarding logic be delivered? (new
  `/onboard-repo` skill recommended)
- **decision-8** — What does the onboard skill do when it can't write
  to the target repo? (stdout + offer-to-append-user-file recommended)
- **decision-9** — List-merge semantics for layered fields?
  (replace-by-default-no-extends recommended)
- **decision-10** — Should the loader implicitly trust repo-defaults or
  require operator opt-in? (auto-discover recommended)
- **decision-11** — Should the validator be strict or advisory?
  (two-tier errors-vs-warnings recommended)
- **decision-12** — Ship a one-shot migration helper alongside this
  work? (yes — `egg config migrate` recommended)
- **decision-13** — Schema versioning on the new files from day one?
  (yes on both files recommended)
- **decision-14** — Languages for the onboard skill detector? (Python +
  Node + Go for v1 recommended; Rust/Java/Ruby in a follow-up)

### Open-ended feedback (registered via `egg-contract add-feedback`)

- **feedback-1** — Seven free-form questions covering: upgrade-friction
  tolerance, repos that need the layered path applied at rollout time,
  priority ranking among the four pieces, whether to reserve a `template:`
  hook for the deferred template-library work, additional validation
  checks worth surfacing, onboard-skill confirmation behavior, and
  non-Makefile/non-conventional check-command patterns in the fleet.

## Complexity Assessment

**high.** Touches:

- A schema both runtimes (host launcher and in-container build) parse
  through ~15 callers across `config/`, `gateway/`, `orchestrator/`,
  `sandbox/egg_lib/`, `scripts/`, and `integration_tests/`.
- A new file location (`<repo>/.egg/repositories.yaml`) that the loader,
  the gateway, and the sandbox build all need to find and merge
  consistently.
- A new validator with a non-trivial heuristic surface
  (lockfile/manifest detection, persist-vs-build-context coherence,
  Makefile target existence, network-mode-aware command checks).
- A new skill (`/onboard-repo`) with multi-language detection and a
  fallback path when the repo isn't writable.
- An optional migration helper that has to rewrite legacy schemas
  in-place safely.
- A backward-compatibility window where both schemas load.

The cross-cutting nature (schema + loader + validator + skill +
migration) and the multiple decision points (several with no obvious
default) put this firmly in the **high** complexity bucket. The plan
phase should expect to break the work into ≥3 independently shippable
phases, with the schema+validator landing first and the skill landing
on top.

---

*Authored-by: egg*
