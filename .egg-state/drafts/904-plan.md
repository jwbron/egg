# Implementation Plan: Build-time dependency installation via build_commands

> Issue: #904 | Phase: plan | Pipeline: issue-904

## Summary

In private mode, sandbox containers have no network access beyond the Anthropic API,
so runtime dependency installation (`pip install`, `npm ci`, etc.) fails. This plan
implements build-time dependency installation by adding a `build_commands` section to
per-repo settings in `repositories.yaml`. Watch files (lockfiles, requirements.txt)
are copied into the Docker build context to enable layer caching, and user-defined
commands run during image build to bake dependencies into the single `egg` image.

## Approach

**Single PR with four implementation phases** within one commit stream:

1. **Config layer** — Add `build_commands` schema to `repositories.yaml.example` and
   parsing functions to `config/repo_config.py`. This is the data foundation.
2. **Build context and hash** — Extend `create_dockerfile()` to copy watch files and
   generate a `build-commands.json` manifest in the build context. Extend
   `compute_build_hash()` to include watch file contents and build commands config.
3. **Build execution** — Extend `docker-setup.py` to read the manifest and execute
   build commands. Add the corresponding Dockerfile layer.
4. **Tests and docs** — Add unit tests for all new functions and update documentation.

Each phase builds on the prior one. Repos without `build_commands` are completely
unaffected (backwards compatible).

## Key Design Decisions

Per the architect's analysis (DD-1 through DD-6):

- **Repo-to-path mapping** (DD-1): Match the repo name segment after `/` against
  `local_repos.paths` directory names. Skip with a warning if no match.
- **Dependency artifacts go to system paths** (DD-2): `pip install` goes to system
  site-packages. `npm ci` warms the npm cache (node_modules in the build dir won't
  persist, but cached packages make runtime install near-instant).
- **Non-fatal failures** (DD-3): Build command failures are logged but don't abort
  the image build, matching the existing `extra_packages` behavior (`check=False`).
- **Dockerfile layer placement** (DD-4): The new layer goes after all toolchain
  installations (Python deps, pip packages) but before the frequently-changing
  `COPY . /opt/egg-runtime/` layer, maximizing cache reuse.
- **Build-commands.json manifest** (DD-6): `create_dockerfile()` generates a minimal
  JSON manifest containing only build-relevant data. `docker-setup.py` reads this
  manifest instead of parsing the full `repositories.yaml` (which may contain
  sensitive data and isn't reliably available during build). The manifest also
  contains the working directory path for each repo's commands.

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Build commands fail in watch-file-only directory | Document that working directory contains only watch_files. Provide examples of what works (pip install -r, npm ci) vs. what doesn't (make with source deps). |
| Runtimes not installed when build_commands run | Dockerfile layer placement ensures Python is always available. Document that Node/Go must be in `extra_packages` if needed by build commands. |
| Config not available during Docker build | Use build-commands.json manifest approach (DD-6). Only build-relevant data is extracted and COPY'd into the build context. |
| Stale deps if user forgets to rebuild | Build hash system handles this automatically — `compute_build_hash()` includes watch file contents, so `egg` detects changes and triggers a rebuild. |
| Security of arbitrary build commands | Commands come from user's own trusted `repositories.yaml`. Same trust model as existing `extra_packages`. Build runs in Docker with no host secret access. |

## Test Strategy

1. **Config tests** (`tests/config/test_repo_config.py`):
   - `get_repo_build_commands()` returns correct structure for valid config
   - Returns `None`/empty for repos without `build_commands`
   - Case-insensitive repo matching works
   - Malformed entries (missing keys, wrong types) are handled gracefully

2. **Docker build context tests** (`tests/sandbox/test_docker.py`):
   - Watch files are copied to correct paths in build context
   - `build-commands.json` manifest is generated with correct structure
   - Missing watch files produce warnings but don't fail
   - Repos with no local path match are skipped
   - Build hash changes when watch files change
   - Build hash changes when build_commands config changes
   - Build hash is stable when nothing changes

3. **Build execution tests** (`tests/sandbox/test_docker_setup.py`):
   - `get_build_commands()` parses manifest correctly
   - `install_build_commands()` runs commands in correct directory
   - Command failures are non-fatal (logged, not raised)
   - Empty manifest means no commands run (no-op)

4. **Existing tests**: Run full test suite to verify no regressions.

## File Impact

| File | Change | Risk |
|------|--------|------|
| `config/repositories.yaml.example` | Add `build_commands` with `watch_files` and `commands` sub-keys to `repo_settings` example | Low |
| `config/repo_config.py` | Add `get_repo_build_commands(repo)` and `get_all_build_commands()` functions | Low |
| `sandbox/egg_lib/docker.py` | Extend `create_dockerfile()` to copy watch files + generate manifest. Extend `compute_build_hash()` to include watch files + build commands. | Medium |
| `sandbox/docker-setup.py` | Add `get_build_commands()` and `install_build_commands()`. Call from `main()`. | Medium |
| `sandbox/Dockerfile` | Add COPY + RUN layer for repo-deps and build-commands manifest | Low |
| `tests/config/test_repo_config.py` | Add `TestGetRepoBuildCommands` and `TestGetAllBuildCommands` test classes | Low |
| `tests/sandbox/test_docker.py` | Add tests for watch file copying, manifest generation, and build hash extension | Low |
| `tests/sandbox/test_docker_setup.py` | Add tests for `get_build_commands()` and `install_build_commands()` | Low |
| `sandbox/README.md` | Document `build_commands` feature and caching behavior | Low |

---

```yaml
# yaml-tasks
pr:
  title: "Add build-time dependency installation via build_commands config"
  description: |
    In private mode, sandbox containers cannot install dependencies at runtime.
    This adds a build_commands configuration to per-repo settings in
    repositories.yaml, allowing users to specify dependency installation
    commands (e.g., npm ci, pip install -r requirements.txt) that run during
    the Docker image build. Watch files enable Docker layer caching so deps
    only rebuild when lockfiles change. All repos' dependencies coexist in
    the single egg image.
phases:
  - id: 1
    name: Config layer
    goal: Add build_commands configuration schema and parsing functions
    tasks:
      - id: TASK-1-1
        description: Add build_commands example to repo_settings section in repositories.yaml.example with watch_files (list of strings) and commands (list of strings) sub-keys, including examples for Python and Node projects
        acceptance: repositories.yaml.example contains a build_commands example under repo_settings with watch_files and commands sub-keys. Comments explain the purpose and caching behavior.
        files:
          - config/repositories.yaml.example
      - id: TASK-1-2
        description: Add get_repo_build_commands(repo) to config/repo_config.py that returns the build_commands dict (with watch_files and commands) for a given repo, or None if not configured. Add get_all_build_commands() that returns a dict mapping repo names to their build_commands. Include validation that watch_files and commands are lists of strings.
        acceptance: get_repo_build_commands("org/repo") returns {"watch_files": [...], "commands": [...]} for configured repos and None for unconfigured. get_all_build_commands() returns all repos with build_commands. Invalid entries (wrong types, missing keys) are filtered out with warnings.
        files:
          - config/repo_config.py
  - id: 2
    name: Build context and hash
    goal: Copy watch files into build context, generate manifest, and extend build hash
    tasks:
      - id: TASK-2-1
        description: "Extend create_dockerfile() in sandbox/egg_lib/docker.py to: (1) import and call get_all_build_commands() from config/repo_config.py, (2) resolve each repo name to a local path by matching the name segment after '/' against get_local_repos() directory names, (3) for each matched repo, copy its watch_files from the local repo path into ~/.cache/egg/repo-deps/<repo-name>/, (4) generate a build-commands.json manifest in ~/.cache/egg/sandbox/ containing the repo name, commands list, and working directory path (/tmp/repo-deps/<repo-name>/) for each repo"
        acceptance: Running create_dockerfile() with build_commands configured copies watch files to the build context and produces a valid build-commands.json manifest. Missing watch files produce warnings. Repos with no local path match are skipped.
        files:
          - sandbox/egg_lib/docker.py
      - id: TASK-2-2
        description: "Extend compute_build_hash() in sandbox/egg_lib/docker.py to: (1) hash the contents of all watch files in the build context repo-deps/ directory (if it exists), (2) hash the build_commands configuration itself (serialized to a stable JSON string). This ensures changes to watch files or commands trigger a rebuild."
        acceptance: Build hash changes when any watch file content changes. Build hash changes when build_commands config changes (added/removed/modified commands). Build hash is stable when nothing changes. Hash computation does not fail when no build_commands are configured.
        files:
          - sandbox/egg_lib/docker.py
  - id: 3
    name: Build execution
    goal: Execute build commands during Docker image build
    tasks:
      - id: TASK-3-1
        description: "Add to sandbox/docker-setup.py: (1) get_build_commands() function that reads /tmp/build-commands.json manifest and returns a list of {repo, commands, workdir} entries, (2) install_build_commands(entries) function that iterates entries, creates workdir if needed, cd's to it, and runs each command via subprocess.run(shell=True, check=False) with stdout/stderr logging, (3) call install_build_commands() from main() after install_extra_packages()"
        acceptance: docker-setup.py reads the build-commands.json manifest, runs each command in the correct working directory, logs output, and continues on failure (non-fatal). When no manifest exists, the function is a no-op.
        files:
          - sandbox/docker-setup.py
      - id: TASK-3-2
        description: "Add a new layer to sandbox/Dockerfile after the Python dependencies section (after line 104) and before the Claude commands COPY (line 111): (1) COPY repo-deps/ directory to /tmp/repo-deps/, (2) COPY sandbox/build-commands.json to /tmp/build-commands.json, (3) No separate RUN needed — docker-setup.py already runs and will call install_build_commands(). Instead, move the docker-setup.py execution to AFTER the repo-deps COPY so it has access to both the config and the watch files. Or add a second RUN of docker-setup.py with a --build-commands flag."
        acceptance: Dockerfile has a layer that COPYs repo-deps/ and build-commands.json before build commands are executed. Docker layer caching works — changing a watch file rebuilds the dependency layer but not earlier layers. The layer is positioned after toolchain installations.
        files:
          - sandbox/Dockerfile
  - id: 4
    name: Tests and documentation
    goal: Add test coverage for all new functionality and update documentation
    tasks:
      - id: TASK-4-1
        description: Add TestGetRepoBuildCommands and TestGetAllBuildCommands test classes to tests/config/test_repo_config.py. Test valid config, missing config, case-insensitive matching, malformed entries (wrong types, missing keys), and empty build_commands.
        acceptance: Tests cover happy path, missing config, case sensitivity, and validation. All tests pass.
        files:
          - tests/config/test_repo_config.py
      - id: TASK-4-2
        description: Add tests to tests/sandbox/test_docker.py for watch file copying in create_dockerfile() and build hash extension. Test that watch files are copied to correct build context paths, manifest is generated correctly, missing files produce warnings, and build hash includes watch file contents and build_commands config.
        acceptance: Tests verify file copying, manifest structure, graceful handling of missing files, and hash sensitivity to watch files and config changes.
        files:
          - tests/sandbox/test_docker.py
      - id: TASK-4-3
        description: Add tests to tests/sandbox/test_docker_setup.py for get_build_commands() and install_build_commands(). Mock subprocess.run. Test manifest parsing, command execution in correct directory, non-fatal failure handling, and no-op when manifest is missing.
        acceptance: Tests verify manifest parsing, correct working directory, non-fatal failures, and empty/missing manifest handling. All tests pass.
        files:
          - tests/sandbox/test_docker_setup.py
      - id: TASK-4-4
        description: Update sandbox/README.md to document the build_commands feature — configuration format, caching behavior, limitations (watch-file-only working directory), and examples for Python and Node projects.
        acceptance: README.md has a section documenting build_commands with configuration examples, explanation of Docker layer caching, and notes about the working directory limitation.
        files:
          - sandbox/README.md
      - id: TASK-4-5
        description: Run the full test suite (make test or equivalent) and verify no regressions.
        acceptance: All existing and new tests pass.
        files: []
```

---

*Authored-by: egg*
