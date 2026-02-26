# Plan: Build-time dependency installation with Docker layer caching

> Issue: #904 | Phase: plan | Pipeline: issue-904

## Summary

In private mode, the sandbox container has no network access beyond the Anthropic API, so runtime dependency installation (`pip install`, `npm ci`, etc.) fails. This plan implements build-time dependency installation via a new `build_commands` config in `repositories.yaml`. User-configured commands run during `docker build` and their results are baked into the single `egg` image.

The recommended approach (from architect analysis, Option B) dynamically generates per-repo `COPY`+`RUN` Dockerfile layers from config, maximizing Docker layer caching — changing one repo's lockfile only rebuilds that repo's dependency layer. As a side effect, this also fixes the existing bug where `docker_setup.extra_packages` is silently ignored because `repositories.yaml` is inaccessible during the Docker build.

## Approach

**Dynamic Dockerfile generation with per-repo layers.** `create_dockerfile()` in `docker.py` already assembles a build context at `~/.cache/egg/`. We extend it to:

1. Read `build_commands` config for all repos via new accessor functions in `repo_config.py`
2. Copy each repo's watch files from `local_repos.paths` into the build context at `repo-deps/<name>/`
3. Inject per-repo `COPY repo-deps/<name>/ /tmp/repo-deps/<name>/` + `RUN cd /tmp/repo-deps/<name> && <commands>` lines into the Dockerfile, after system packages/pip installs but before the runtime scripts copy
4. Include watch file contents in `compute_build_hash()` so dependency file changes trigger rebuilds

A `# === DEPENDENCY_LAYERS ===` marker comment is added to the static Dockerfile to make the insertion point explicit and resilient to refactoring.

## Key Design Decisions

These are from the architect analysis and inform the implementation:

- **DD-1: Volume overlay limitation** — Document that local-installing package managers (npm → `node_modules/`) lose build-time artifacts when runtime volumes overlay the path. Recommend `pip` (installs globally) or `--prefix /opt/deps/<name>` for npm.
- **DD-2: Execution context** — Build commands run as root in `/tmp/repo-deps/<name>/` (standard Docker build context). The `egg` user doesn't exist yet at the insertion point.
- **DD-3: Failure handling** — Build commands fail the Docker build on error (standard Docker behavior). Users can add `|| true` to individual commands if desired.
- **DD-4: Config accessibility fix** — Extract `docker_setup` section to a minimal YAML file in the build context, fixing the pre-existing `extra_packages` bug.
- **DD-5: Repo-name matching** — Match `repo_settings` keys (owner/repo) to `local_repos.paths` by directory basename.
- **DD-6: Insertion point** — After pip installs (line 104) and before `COPY . /opt/egg-runtime/` (line 126). Use a comment marker.

## Implementation Phases

### Phase 1: Config Schema & Accessors

**Goal:** Establish the `build_commands` config schema and provide accessor functions so subsequent phases can consume the config.

**[TASK-1-1] Add build_commands accessor functions to repo_config.py**

Add two new functions following the existing `get_repo_setting()` pattern at `config/repo_config.py:244`:

- `get_repo_build_commands(repo: str) -> dict | None` — returns `build_commands` dict (`watch_files`, `commands`, optional `env`) for a given repo, or `None`.
- `get_all_build_commands() -> dict[str, dict]` — returns `{repo: build_commands}` for all repos that have the setting. Iterates `repo_settings` keys.

Both use `_load_config()` internally (existing pattern). The schema for `build_commands`:
```yaml
build_commands:
  watch_files: [str]   # relative paths within repo
  commands: [str]      # shell commands to run
  env: {str: str}      # optional env vars (deferred if scope concern)
```

Acceptance: functions return correct data from test config; `None`/`{}` for unconfigured repos.

**[TASK-1-2] Update repositories.yaml.example with build_commands examples**

Add documented `build_commands` examples under the existing `repo_settings` section in `config/repositories.yaml.example`. Include examples for npm, pip, and make-based projects, with comments explaining watch_files, commands, and the volume overlay limitation.

Acceptance: example file is valid YAML, includes at least two `build_commands` examples with comments.

---

### Phase 2: Build Context & Dockerfile Generation

**Goal:** Implement the core feature — copy watch files into the build context, generate per-repo Dockerfile layers, and extend the build hash to detect dependency changes.

**[TASK-2-1] Add Dockerfile insertion marker**

Add `# === DEPENDENCY_LAYERS ===` comment to the static `sandbox/Dockerfile` after the Phase 2 pip install block (after line 104, before the "Note: Claude authentication" comment at line 106). This is the anchor point for dynamic layer injection.

Acceptance: marker present in Dockerfile; existing build behavior unchanged (marker is just a comment).

**[TASK-2-2] Fix extra_packages config accessibility during build**

Fix the pre-existing bug where `docker-setup.py`'s `load_config()` can't find `repositories.yaml` during Docker build:

1. In `create_dockerfile()` (`docker.py`), extract the `docker_setup` section from loaded config and write it as `docker-setup-config.yaml` in the build context (`~/.cache/egg/`).
2. In the Dockerfile, add a `COPY` line to bring this config file into the build at `/tmp/docker-setup-config.yaml` (before the docker-setup.py `RUN` step).
3. In `docker-setup.py:load_config()`, add `/tmp/docker-setup-config.yaml` as the first search path (highest priority in build context).

Acceptance: `docker-setup.py` successfully reads `extra_packages` during build; existing config search order preserved for non-build contexts.

**[TASK-2-3] Copy watch files into build context**

Extend `create_dockerfile()` in `docker.py` to:

1. Call `get_all_build_commands()` from `repo_config.py`
2. For each repo with `build_commands`, resolve the local path by matching the repo basename against `get_local_repos()` paths
3. Copy each watch file from the local repo path into `~/.cache/egg/repo-deps/<repo-name>/`
4. Validate that watch files exist; warn and skip missing files (don't fail the build for optional files)
5. Skip repos entirely if no matching local path is found (warn)

Acceptance: watch files appear in build context under `repo-deps/<name>/`; missing files produce warnings not errors; repos without local paths are skipped.

**[TASK-2-4] Generate per-repo Dockerfile layers**

Extend `create_dockerfile()` to inject dynamic layers:

1. Read the static Dockerfile from the build context
2. Find the `# === DEPENDENCY_LAYERS ===` marker
3. For each repo with `build_commands` (and successfully copied watch files):
   - Generate `COPY repo-deps/<name>/ /tmp/repo-deps/<name>/`
   - If `env` is specified, generate `ENV` lines
   - Generate `RUN cd /tmp/repo-deps/<name> && set -e && <cmd1> && <cmd2> && ...`
4. Insert the generated lines after the marker
5. Write the modified Dockerfile back to the build context

When no repos have `build_commands`, the Dockerfile is unchanged (just has the marker comment).

Shell escaping: Commands are user-provided strings placed directly in `RUN` lines. No additional escaping — users write commands as they would in a shell. Commands are joined with ` && ` and prefixed with `set -e && ` for fail-fast behavior.

Acceptance: generated Dockerfile contains correct `COPY`/`RUN` pairs for each configured repo; empty `build_commands` produces unchanged Dockerfile; `docker build` succeeds with generated layers.

**[TASK-2-5] Extend build hash to include watch files and build config**

Extend `compute_build_hash()` in `docker.py` to include:

1. Content of all watch files from all repos (hash each file)
2. The `build_commands` config values (commands list, env dict) serialized deterministically

This ensures changes to `package-lock.json`, `requirements.txt`, or the build commands themselves trigger image rebuilds.

Acceptance: changing a watch file's content changes the build hash; changing build commands changes the hash; adding/removing repos with build_commands changes the hash.

---

### Phase 3: Tests & Documentation

**Goal:** Comprehensive test coverage and user-facing documentation.

**[TASK-3-1] Add unit tests for config accessors**

Add tests to `tests/config/test_repo_config.py` following existing patterns:

- `get_repo_build_commands()` returns correct dict for configured repo
- `get_repo_build_commands()` returns `None` for unconfigured repo
- `get_all_build_commands()` returns only repos with build_commands
- Case-insensitive repo name matching

Acceptance: all tests pass; cover happy path, missing config, and edge cases.

**[TASK-3-2] Add unit tests for Dockerfile generation and build hash**

Add tests to `tests/sandbox/test_docker.py` following existing patterns (mocked `Context`, `subprocess`, etc.):

- Watch file copying: files copied to correct paths, missing files warned, no matching local repo skipped
- Dockerfile generation: marker found, correct COPY/RUN pairs injected, empty config produces unchanged Dockerfile
- Build hash: watch file content changes hash, config changes hash
- Extra_packages fix: config file written to build context

Acceptance: all tests pass; cover the generation logic without requiring Docker.

**[TASK-3-3] Update documentation**

Update `sandbox/README.md` with:

- `build_commands` feature overview and usage
- Configuration examples (mirror `repositories.yaml.example`)
- Volume overlay limitation explanation and workarounds (pip is fine, npm needs `--prefix`)
- How Docker layer caching works with this feature

Acceptance: docs accurately describe the feature, limitations, and config format.

---

## Test Strategy

- **Unit tests** (Phase 3): All new functions tested in isolation with mocked dependencies. Follows existing pytest + `unittest.mock` patterns in `tests/sandbox/test_docker.py` and `tests/config/test_repo_config.py`.
- **Integration verification**: The coder should manually verify that `create_dockerfile()` produces a correct Dockerfile by inspecting the build context output. No actual Docker build is needed in tests (existing pattern — `test_docker.py` mocks `subprocess.run`).
- **Backwards compatibility**: Tests verify that repos without `build_commands` produce the exact same Dockerfile (modulo the marker comment).

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Shell escaping in user commands | Low | Medium | Don't escape — users write raw shell commands. Document that commands run in `sh -c`. |
| Insertion marker drift after Dockerfile edits | Low | High | Marker is explicit; `create_dockerfile()` raises error if marker not found. |
| Watch file paths outside repo | Low | Medium | Validate files are under the local repo path (path traversal check). |
| Config import cycle (docker.py → repo_config.py) | Low | Medium | `repo_config.py` is in `config/`, already independent of `sandbox/egg_lib/`. Import is straightforward. |

## Dependency Ordering

```
TASK-1-1 ─┬─→ TASK-2-3 → TASK-2-4
           ├─→ TASK-2-5
           └─→ TASK-3-1
TASK-1-2 (parallel with TASK-1-1)
TASK-2-1 ──→ TASK-2-4
TASK-2-2 (parallel with TASK-2-1)
TASK-3-1, TASK-3-2, TASK-3-3 (after all Phase 2 tasks)
```

Critical path: **TASK-1-1 → TASK-2-3 → TASK-2-4** (config → watch files → Dockerfile generation)

```yaml
# yaml-tasks
pr:
  title: "Add build-time dependency installation with Docker layer caching"
  description: |
    Support build-time dependency installation via user-configured
    build_commands in repositories.yaml. Per-repo watch files (lockfiles,
    requirements.txt) are copied into the Docker build context, and
    per-repo COPY+RUN layers are dynamically generated in the Dockerfile
    for optimal Docker layer caching. Also fixes the pre-existing bug
    where docker_setup.extra_packages was silently ignored during builds.
phases:
  - id: 1
    name: Config Schema & Accessors
    goal: Establish build_commands config schema and accessor functions
    tasks:
      - id: TASK-1-1
        description: Add get_repo_build_commands() and get_all_build_commands() accessor functions to repo_config.py, following the existing get_repo_setting() pattern
        acceptance: Functions return correct build_commands dict for configured repos and None/{} for unconfigured repos
        files:
          - config/repo_config.py
      - id: TASK-1-2
        description: Update repositories.yaml.example with documented build_commands examples for npm, pip, and make-based projects
        acceptance: Example file is valid YAML with at least two build_commands examples and comments explaining usage and limitations
        files:
          - config/repositories.yaml.example
  - id: 2
    name: Build Context & Dockerfile Generation
    goal: Implement watch file copying, dynamic Dockerfile layer generation, build hash extension, and fix extra_packages bug
    tasks:
      - id: TASK-2-1
        description: "Add # === DEPENDENCY_LAYERS === marker comment to sandbox/Dockerfile after the Phase 2 pip install block (after line 104)"
        acceptance: Marker present in Dockerfile; existing image build behavior unchanged
        files:
          - sandbox/Dockerfile
      - id: TASK-2-2
        description: Fix extra_packages config accessibility by extracting docker_setup config to build context and updating docker-setup.py load_config() search paths
        acceptance: docker-setup.py successfully reads extra_packages during docker build
        files:
          - sandbox/egg_lib/docker.py
          - sandbox/docker-setup.py
          - sandbox/Dockerfile
      - id: TASK-2-3
        description: Extend create_dockerfile() to copy watch files from local repo paths into build context at repo-deps/<name>/, with repo-name matching and validation
        acceptance: Watch files appear in build context; missing files produce warnings; repos without local paths are skipped
        files:
          - sandbox/egg_lib/docker.py
      - id: TASK-2-4
        description: Extend create_dockerfile() to find the DEPENDENCY_LAYERS marker and inject per-repo COPY+RUN Dockerfile instructions for each repo with build_commands
        acceptance: Generated Dockerfile contains correct COPY/RUN pairs; empty build_commands produces unchanged Dockerfile
        files:
          - sandbox/egg_lib/docker.py
      - id: TASK-2-5
        description: Extend compute_build_hash() to include watch file contents and build_commands config in the SHA256 hash
        acceptance: Changing watch file content or build commands config changes the computed build hash
        files:
          - sandbox/egg_lib/docker.py
  - id: 3
    name: Tests & Documentation
    goal: Comprehensive test coverage and user-facing documentation
    tasks:
      - id: TASK-3-1
        description: Add unit tests for get_repo_build_commands() and get_all_build_commands() to tests/config/test_repo_config.py
        acceptance: Tests pass covering happy path, missing config, empty config, and case-insensitive matching
        files:
          - tests/config/test_repo_config.py
      - id: TASK-3-2
        description: Add unit tests for watch file copying, Dockerfile generation, build hash extension, and extra_packages fix to tests/sandbox/test_docker.py
        acceptance: Tests pass covering generation logic, marker detection, empty config, and hash changes
        files:
          - tests/sandbox/test_docker.py
      - id: TASK-3-3
        description: Update sandbox/README.md with build_commands feature documentation including usage, examples, volume overlay limitation, and workarounds
        acceptance: Documentation accurately describes the feature, config format, limitations, and workarounds
        files:
          - sandbox/README.md
```

*Authored-by: egg*
