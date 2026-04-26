# Repository Configuration: Layered Repo + User Defaults

> Status: introduced by [issue #2073](https://github.com/jwbron/egg/issues/2073).
> Hard cutover — legacy `persist_dirs` / `persist_system_dirs` /
> explicit-only `watch_files` keys are no longer accepted in the user-facing
> schema.

This guide explains how egg loads repository configuration after the
schema simplification + layered config rollout. It covers where each file
lives, what merge semantics apply, how to author a
`<repo>/.egg/repositories.yaml`, how to run the validator, and the
migration steps for existing operators.

## Why a layered model

Onboarding a new repo to egg used to mean hand-authoring a per-repo block
in `~/.config/egg/repositories.yaml`. That block carried four kinds of
information:

1. **Operator-scoped policy** — does this operator have write access?
   Should we restrict auto-responses to trusted users? Default reviewer?
2. **Repo-scoped build shape** — `build_commands`, `persist`,
   `watch_files`, `checks`. These are deterministic properties of the
   repo, not the operator.
3. **Repo-scoped policy** — `auth_mode`, `checkpoint_repo`. Mostly
   repo-deterministic, occasionally operator-overridden.
4. **Forward-looking knobs** — `template:` (reserved).

Categories (2)–(4) are repo-deterministic but used to live only on each
operator's machine, so every onboarder rediscovered the same per-repo
block. Worse, several traps in the schema only surfaced at runtime, weeks
after broken config had been baked into a sandbox image — see
[#2065](https://github.com/jwbron/egg/issues/2065),
[#2087](https://github.com/jwbron/egg/issues/2087), and
[#2090](https://github.com/jwbron/egg/issues/2090) for representative
reproducers.

The new layered model splits the load across two files:

| File | Scope | Checked into | Authored by |
|------|-------|--------------|-------------|
| `<repo>/.egg/repositories.yaml` | Repo defaults: `build_commands`, `persist`, `watch_files`, `checks`, `auth_mode`, `checkpoint_repo`, `template` | The target repo | [`/onboard-repo`](../../skills/onboard-repo/SKILL.md) (or hand-authored) |
| `~/.config/egg/repositories.yaml` | Operator state: `github_username`, `bot_username`, `writable_repos`, `readable_repos`, `local_repos`, `default_reviewer`, `restrict_to_configured_users`, `disable_auto_fix`, `repo_settings.<repo>.<override>` | Not committed (per-operator) | [`/egg-setup --update repos`](../../skills/egg-setup/SKILL.md) |

Both files carry `schemaVersion: "1.0"` from day one
([decision-13](../../.egg-state/contracts/issue-2073.json)).

## Merge semantics

The single merge entry point is
`shared.egg_config.repos.load_merged_repo_config(checkout_path,
user_path)`. It is imported by `config/repo_config.py` and
`sandbox/egg_lib/docker.py`, so every host-side consumer of repo config
sees the same merged dict.

The merge rules are:

1. **Auto-discovery** — the loader silently looks for
   `<checkout>/.egg/repositories.yaml` whenever it is given a checkout
   path. Absent file → no-op. Present file → loaded as the **repo
   defaults**. ([decision-10](../../.egg-state/contracts/issue-2073.json))
2. **User file always loaded** — `~/.config/egg/repositories.yaml` (or
   `$EGG_REPO_CONFIG`) supplies operator-scoped state.
3. **Per-repo deep-merge** — for each repo in
   `repo_settings.<owner/repo>`, the repo-defaults block is merged first,
   the user-overrides block applied on top, **user wins at the leaf**.
4. **Replace-by-default for list-valued fields** — `persist`,
   `watch_files`, `checks`, `extra_packages.apt`, `extra_packages.dnf`,
   and `local_repos.paths` are **replaced** outright by the user file
   when both define them.
   ([decision-9](../../.egg-state/contracts/issue-2073.json) — no
   `extends:` keyword in v1; revisit if duplication shows up across the
   fleet.)
5. **Operator-scoped keys stay in the user file** — `github_username`,
   `bot_username`, `writable_repos`, `readable_repos`, `local_repos`,
   `default_reviewer`, `user_mode`, `github_sync`, `docker_setup`,
   `restrict_to_configured_users`, `disable_auto_fix` are **rejected**
   when found in `<repo>/.egg/repositories.yaml`. The diagnostic names
   the offending key and points the user at the correct file.

The loader uses `functools.lru_cache` keyed by `(mtime, path)` tuples so
repeated calls within a process don't re-scan the filesystem.
Invalidation is wired into the existing `config/repo_config.reload_config`
SIGHUP hook ([risk-13](../../.egg-state/drafts/2073-plan.md#risks--mitigations)).

### Why replace-by-default

Append-merge is the most ergonomic for additive cases (e.g., the operator
wants to add an extra check on top of repo defaults), but it is also the
most surprising — a typo in the user file silently piles up rather than
overriding. Per
[issue #2073](https://github.com/jwbron/egg/issues/2073)'s open-question
discussion, **replace** is simpler to teach and matches the expectation
"my override file takes precedence." If duplication shows up across the
fleet, a follow-up can add an explicit `extends: true` opt-in without
breaking existing configs.

## Authoring `<repo>/.egg/repositories.yaml`

The recommended on-ramp is the [`/onboard-repo`](../../skills/onboard-repo/SKILL.md)
skill — it detects the repo's build shape, confirms every entry with the
user, runs `egg validate-config` pre-flight, and writes the block.

If you'd rather hand-author, use this template:

```yaml
schemaVersion: "1.0"

# Reserved for a future template library (issue #2073 Q4). Accepts
# `string | null`. Leave unset for now.
template: null

# Build-time dependency installation. The host-side classifier in
# `config/repo_config.py` routes entries in `persist:` whose first
# character is `/` to system-absolute paths, others to repo-relative
# paths. The classifier produces the legacy `persist_dirs` +
# `persist_system_dirs` two-list shape; `sandbox/egg_lib/docker.py`
# then writes that into `manifest.json`, so `sandbox/docker-setup.py`
# is unchanged — schema simplification is a host-side concern.
build_commands:
  watch_files:
    # Files that trigger a rebuild when changed (e.g., lockfiles). When
    # omitted, the loader infers them from a built-in catalog of known
    # manifests (pyproject.toml, uv.lock, package.json, pnpm-lock.yaml,
    # package-lock.json, yarn.lock, go.mod, go.sum, Cargo.toml,
    # Cargo.lock, requirements*.txt, Gemfile.lock).
    - pyproject.toml
    - uv.lock
  commands:
    # Run as root in a directory seeded with the watch_files. For uv /
    # pip / npm-install commands paired with a watch-files-only build
    # context (the #2087 trap), pass `--no-install-project` /
    # `--no-deps` so the install doesn't try to reach into source files
    # the build context didn't copy.
    - uv sync --frozen --no-install-project

# Unified persist list. Entries beginning with `/` are absolute system
# paths (host classifier routes them to `persist_system_dirs` in the
# manifest); others are repo-relative (routed to `persist_dirs`). The
# loader enforces a hard-error denylist on entries declared in this
# file (paths under /etc, /root, /var, /home/, /proc, /sys, /dev,
# /.ssh, and any absolute path outside /usr/local/, /opt/, or the repo
# root).
persist:
  - .venv               # repo-relative
  - /usr/local/bin/uv   # system-absolute

# Pipeline check commands. When omitted, the loader infers them from
# `Makefile` lint/test targets and `package.json` scripts. Explicit
# entries here win.
checks:
  - {name: lint, command: make lint}
  - {name: test, command: make test}

# Optional per-repo policy.
auth_mode: bot                    # or "user" — uses GITHUB_USER_TOKEN
checkpoint_repo: jwbron/egg-ckpt  # optional — separate transcript repo
```

## `manifest.json` invariant (sandbox stability)

The internal sandbox manifest at `<config-dir>/repo-deps/manifest.json`
(a single top-level file, not per-repo — see
`sandbox/egg_lib/docker.py::_copy_repo_watch_files`) keeps its
**pre-#2073 shape**:

```json
{
  "extra_packages": {"apt": [...], "dnf": [...]},
  "build_commands": [
    {
      "repo": "<owner/repo>",
      "watch_files": [...],
      "commands": [...],
      "persist_dirs": [...],
      "persist_system_dirs": [...]
    }
  ]
}
```

That is, a top-level `extra_packages` block sits alongside a
`build_commands` list, with each list entry carrying a `repo` field plus
the four per-repo arrays. The per-repo subdirectories under
`<config-dir>/repo-deps/<repo-dir-name>/` only contain copied watch
files (no per-repo manifest).

The host-side classifier in `config/repo_config.py` produces
`persist_dirs` + `persist_system_dirs` from the unified `persist:` list,
which `sandbox/egg_lib/docker.py::_copy_repo_watch_files` then writes
into `manifest.json` (architect C3 design;
[risk-1 mitigation](../../.egg-state/drafts/2073-plan.md#risks--mitigations))
so `sandbox/docker-setup.py` is unchanged and existing sandbox images
keep working against the new launcher without forcing a rebuild. **Do
not** write `persist_dirs` / `persist_system_dirs` into the user-facing
YAML — the loader rejects them. They live only in the manifest.

## Validator checks

`egg validate-config <path>` (and the `mcp__egg__validate_repo_config`
MCP tool) emit two-tier output
([decision-11](../../.egg-state/contracts/issue-2073.json)):

| # | Check | Severity | Notes |
|---|-------|----------|-------|
| a | `build_commands` install something to a system path but no `persist:` covers it | **Error** | Covers the [#2065](https://github.com/jwbron/egg/issues/2065) class — install to `/usr/local/bin/<name>` without a covering entry. |
| b | `uv sync` / `pip install -e .` / `npm install` paired with watch-files-only build context | Warning | Covers [#2087](https://github.com/jwbron/egg/issues/2087); suggests `--no-install-project` / `--no-deps`. |
| c | `checks.command` references a missing `Makefile` target | **Error** | Catches drift between `checks:` and the Makefile. |
| d | `watch_files` lists a manifest the `build_commands` don't appear to use, OR omits one they do | Warning | Heuristic only. |
| e | `local_repos.paths` entry doesn't exist on disk | **Error** | Operator-scoped (user file). |
| f | `writable_repos` entry not under `repo_settings` | Warning | Operator-scoped (user file). |
| g | `checkpoint_repo` is unreachable | Warning | Best-effort; network failures don't block. |
| h | `<repo>/.egg/repositories.yaml` contains operator-scoped keys | **Error** | Surfaces the loader's rejection cleanly. |
| i | `auth_mode: user` without `GITHUB_USER_TOKEN` configured at validate-time | Warning | Runtime injection paths exist (env var, secrets manager, CI variable). |
| j | `persist:` entry pointing to an empty directory | Warning | Common typo / stale config. |
| k | `build_commands` exec `curl` / `wget` while egg's network-locked private mode is in effect | Warning | Network-mode condition only — independent of `restrict_to_configured_users`. |

Errors block the load (or the `/onboard-repo` write); warnings advise
but do not block. Run periodically to catch drift as the build / persist
/ checks shape evolves.

### CLI usage

The validator is exposed via `scripts/validate-config.py --repo-config
<path>` (TASK-4-2), wrapped by the launcher as `egg validate-config
<path>`:

```bash
# Validate the repo file at the current checkout
egg validate-config .

# Validate an explicit path
egg validate-config /path/to/repo
```

Exit code: `0` on a clean config (no errors; warnings are stdout-only),
`1` on errors. The merge layer also pulls in the user file, so checks
against operator-scoped fields (`local_repos.paths` existence,
`writable_repos` cross-references) surface from the same invocation.

### MCP tool usage

The new MCP tool `mcp__egg__validate_repo_config` returns a structured
`{ok: bool, errors: [...], warnings: [...]}`. Agents can call it before
proposing an edit to the layered files. The existing
`mcp__egg__validate_config` (which validates pipeline configs) is **not
renamed** — it stays under its original name to avoid breaking external
callers (architect Q-A6).

## Auto-discovery trust model

When egg encounters `<repo>/.egg/repositories.yaml` in a checkout for
the first time, it **silently trusts** the file
([decision-15](../../.egg-state/contracts/issue-2073.json)). The
security floor is a hard-error denylist enforced by the loader on
`persist:` entries declared in the repo file:

- Paths under `/etc`, `/root`, `/var`, `/home/`, `/proc`, `/sys`,
  `/dev`, `/.ssh` are rejected outright.
- Any absolute path outside `/usr/local/`, `/opt/`, or the repo root
  is rejected.
- Operator-scoped policy keys (`restrict_to_configured_users`,
  `disable_auto_fix`) are rejected when present in the repo file.

Operators retain full freedom in the user file — these constraints
apply only to repo-defaults checked into source control, where a
malicious feature branch could otherwise smuggle in a
`persist: [/etc/passwd]` entry. `build_commands` still run as root in
the sandbox; the denylist is a defense-in-depth measure that limits the
blast radius of a compromised repo-defaults file.

## Migration steps for existing operators

> Hard cutover. Existing user files **must** be hand-edited on upgrade —
> the legacy keys (`persist_dirs`, `persist_system_dirs`, explicit
> `watch_files`) are no longer accepted, and the loader fails fast with
> a diagnostic naming the migration target.

For every per-repo block in your `~/.config/egg/repositories.yaml`:

1. **Merge `persist_dirs` + `persist_system_dirs` into one `persist:` list.**
   Entries that were under `persist_system_dirs` (absolute paths) keep
   their leading `/`; entries that were under `persist_dirs` (repo-
   relative) drop the `./` prefix if any. The host-side classifier will
   route them back to the two-list manifest shape automatically.

   ```yaml
   # Before
   persist_dirs:
     - .venv
   persist_system_dirs:
     - /usr/local/bin/uv

   # After
   persist:
     - .venv
     - /usr/local/bin/uv
   ```

2. **Delete explicit `watch_files` and `checks` blocks where the
   auto-detect output matches.** The loader infers `watch_files` from a
   manifest catalog and `checks` from `Makefile` / `package.json`. Run
   `egg validate-config <repo>` to confirm the inferred output matches
   your expectations before deleting; if it doesn't, keep the explicit
   block.

3. **Add `schemaVersion: "1.0"` at the top** of both your user file and
   any `<repo>/.egg/repositories.yaml` you author.

4. **Delete per-repo blocks for repos that ship their own
   `.egg/repositories.yaml`.** The loader auto-discovers the repo file;
   leaving a stale block under `repo_settings:` will replace the repo
   defaults.

5. **Run `egg validate-config <repo>`** on each repo to confirm the
   migration. The validator surfaces every legacy key with a clear
   diagnostic.

6. **Relaunch egg.** No DB migrations, no service restarts beyond the
   normal egg-launcher relaunch. The internal sandbox manifest format is
   unchanged ([architect C3](../../.egg-state/agent-outputs/2073-architect-output.json))
   so existing images keep working.

### Onboarding new repos

For new repos, run [`/onboard-repo`](../../skills/onboard-repo/SKILL.md)
inside an egg session against a clone of the target repo. The skill
detects Python (uv + pip), Node (npm + pnpm + yarn), and Go projects
([decision-14](../../.egg-state/contracts/issue-2073.json) — Rust /
Java / Ruby deferred until a consumer surfaces) and writes the
`<repo>/.egg/repositories.yaml` after AskUserQuestion confirmation per
detected entry.

## Rollback

If the rollout surfaces a regression in production:

1. **Revert the merge commit** on `main` (single PR per CLAUDE.md, so
   one revert restores pre-#2073 state).
2. **Operator-side recovery** — apply the inverse `sed` snippet below
   against your `~/.config/egg/repositories.yaml` to split `persist:`
   back into `persist_dirs` + `persist_system_dirs` based on
   leading-slash classification. The snippet is intentionally inlined
   here so the rollback path is self-contained; verify against your
   file before running:

   ```bash
   # Approximate inversion — verify against your file before running
   sed -i.bak '/^persist:/,/^[a-z]/{
     s|^  - \(/.*\)$|  __SYS__\1|
     s|^  - \(.*\)$|  __REPO__\1|
   }' ~/.config/egg/repositories.yaml
   # Then split __SYS__ entries under persist_system_dirs: and __REPO__
   # entries under persist_dirs: by hand.
   ```

3. **Image-level recovery** — manifest format is unchanged
   ([architect C3](../../.egg-state/agent-outputs/2073-architect-output.json)),
   so no layer cache invalidation or sandbox-image rebuild is forced.
   Existing sandbox images keep working with the reverted launcher.

## See also

- [`/onboard-repo` skill](../../skills/onboard-repo/SKILL.md) — primary
  on-ramp for authoring `<repo>/.egg/repositories.yaml`.
- [`/egg-setup --update repos`](../../skills/egg-setup/SKILL.md) —
  operator-side flow for the user file.
- [Config README](../../config/README.md) — directory layout, host
  configuration, and `config.yaml` schema reference.
- [`shared/egg_config` README](../../shared/egg_config/README.md) —
  unified configuration framework internals.
- [Issue #2073](https://github.com/jwbron/egg/issues/2073) — original
  proposal and acceptance criteria.
