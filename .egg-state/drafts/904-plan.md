# Implementation Plan: Build-time dependency installation via build_commands

> Issue: #904 | Phase: plan | Pipeline: issue-904 | Agent: task_planner

## Summary

In private mode, sandbox containers have no network access beyond the Anthropic API,
so runtime dependency installation (`pip install`, `npm ci`, etc.) fails. This plan
adds a `build_commands` configuration to per-repo settings in `repositories.yaml`,
allowing users to specify dependency installation commands that run during the Docker
image build. Watch files (lockfiles, requirements.txt) are copied into the build
context to enable Docker layer caching, and all repos' dependencies coexist in the
single `egg` image.

## Approach

**Single PR, four implementation phases.** Each phase builds on the prior one. Repos
without `build_commands` are completely unaffected (backwards compatible).

1. **Config layer** -- Schema example and parsing functions for `build_commands`
2. **Build context and hash** -- Watch file copying (with path traversal validation),
   manifest generation, and build hash extension
3. **Build execution** -- `docker-setup.py --build-commands` flag and new Dockerfile layer
4. **Tests and docs** -- Unit tests for all new functionality (including security) and docs

## Key Design Decisions

From the architect's analysis (DD-1 through DD-8):

- **Repo-to-path mapping (DD-1):** Match the repo name segment after `/` against
  `get_local_repos()` directory names. Skip with a prominent warning if no match.
- **System paths for artifacts (DD-2):** `pip install` goes to system site-packages.
  `npm ci` warms the npm cache. Document the pip vs npm asymmetry.
- **Non-fatal failures (DD-3):** Build command failures are logged prominently (command,
  exit code, stderr) but don't abort the build. Summary printed at end.
- **Layer placement (DD-4):** New layer after PyPI pre-installs (line 104), before
  Claude commands COPY (line 110).
- **Manifest with field allowlisting (DD-6):** `build-commands.json` contains only
  `{repo_name, watch_files, commands}` per repo. Explicit allowlist prevents data leakage.
- **Second docker-setup.py invocation (DD-7):** New Dockerfile layer COPYs docker-setup.py
  and runs it with `--build-commands`. Existing invocation (lines 58-63, OS packages)
  is unchanged. Two layers with different cache invalidation triggers.
- **Path traversal validation (DD-8):** All `watch_files` paths validated with
  `Path.resolve()` + `is_relative_to()`. Paths escaping the repo directory are rejected
  with a clear error. Null bytes rejected.

## Dockerfile Layer (DD-7)

```dockerfile
# After line 104 (PyPI pre-installs), before line 110 (Claude commands):
# --- Phase: Project dependency installation (build_commands) ---
COPY sandbox/repo-deps/ /tmp/repo-deps/
COPY sandbox/build-commands.json /tmp/build-commands.json
COPY sandbox/docker-setup.py /tmp/docker-setup.py
RUN python3 /tmp/docker-setup.py --build-commands \
    && rm -f /tmp/docker-setup.py /tmp/build-commands.json
```

## Risk Mitigation

| Risk | Severity | Mitigation |
|------|----------|------------|
| Path traversal via watch_files (R-6) | High | `Path.resolve()` + `is_relative_to()` validation. Reject with clear error. Test in TASK-4-2. |
| Sensitive data in manifest (R-3) | High | Explicit field allowlisting: only `{repo_name, watch_files, commands}`. |
| npm artifacts lost at runtime (R-1) | Medium | Document pip vs npm asymmetry. Document `--prefer-offline` pattern. |
| Silent build command failures (R-5) | Medium | Prominent logging with command, exit code, stderr. Summary at end. |
| Build hash misses command changes (R-7) | Medium | Hash both watch file contents AND serialized `build_commands` config. |
| Missing watch files (R-12) | Medium | Warn per file. If ALL watch files missing for a repo, skip with warning. |
| Concurrent build context writes (R-10) | Low | Use `_copy_directory_atomic()` pattern for `repo-deps/`. |
| Repo name path resolution fails (R-4) | Low | Prominent warning with repo name and searched paths. |

## Phase Details

### Phase 1: Config layer

**Goal:** Establish the data model for `build_commands` configuration.

**TASK-1-1: Add `build_commands` example to `repositories.yaml.example`**

Add a `build_commands` section under an example repo in `repo_settings` with:
- `watch_files`: list of relative paths to dependency files (lockfiles, requirements.txt)
- `commands`: list of shell commands to run during image build

Include commented examples for Python (`pip install -r requirements.txt`) and Node
(`npm ci`) projects. Add comments explaining Docker layer caching behavior, the
watch-file-only working directory, and that runtimes must be in `extra_packages`.

Files: `config/repositories.yaml.example`

**TASK-1-2: Add `get_repo_build_commands()` and `get_all_build_commands()` to `repo_config.py`**

Add two functions following the existing `get_repo_setting()` pattern:

- `get_repo_build_commands(repo: str) -> dict | None`: Returns `{"watch_files": [...], "commands": [...]}` for a configured repo, or `None` if not configured. Validates that `watch_files` and `commands` are both lists of strings. Filters out malformed entries with logged warnings.

- `get_all_build_commands() -> dict[str, dict]`: Returns a dict mapping repo names to their `build_commands` dicts for all configured repos. Only includes repos with valid `build_commands`.

Both functions use the existing `_load_config()` and case-insensitive repo matching.

Files: `config/repo_config.py`

### Phase 2: Build context and hash

**Goal:** Copy watch files into the Docker build context with security validation and extend the build hash.

**TASK-2-1: Extend `create_dockerfile()` for watch file copying and manifest generation**

Extend `create_dockerfile()` in `sandbox/egg_lib/docker.py` to:

1. Import and call `get_all_build_commands()` from `config.repo_config`
2. Import `get_local_repos()` from the shared config module
3. Resolve each repo name to a local path by matching the name segment after `/` against `get_local_repos()` directory names
4. **Path traversal validation (DD-8):** For each `watch_file` path:
   - Reject paths containing null bytes
   - Compute `(repo_local_path / watch_file).resolve()`
   - Verify `resolved.is_relative_to(repo_local_path.resolve())`
   - Reject paths that escape with `ValueError` naming the offending path and repo
5. For each matched repo, copy its validated watch files from the local repo path into `Config.CONFIG_DIR / "sandbox" / "repo-deps" / <repo-name>/`
6. Handle missing watch files: warn per file. If ALL watch files for a repo are missing, skip that repo with a prominent warning
7. Generate `build-commands.json` manifest in `Config.CONFIG_DIR / "sandbox/"` with **explicit field allowlisting**: only `{repo_name, commands, watch_files}` per entry. Do NOT serialize the full `repo_settings` dict
8. When no repos have `build_commands`, create an empty `repo-deps/` directory and an empty-list manifest so the Dockerfile COPY doesn't fail

Use `_copy_directory_atomic()` or an equivalent atomic pattern for the `repo-deps/` directory to handle concurrent egg instances (R-10).

Files: `sandbox/egg_lib/docker.py`

**TASK-2-2: Extend `compute_build_hash()` to include watch files and build commands**

Extend `compute_build_hash()` in `sandbox/egg_lib/docker.py` to:

1. After existing hash computation, check for `repo-deps/` in the build context (`Config.CONFIG_DIR / "sandbox" / "repo-deps"`)
2. If it exists, hash all files in it (recursively, sorted for determinism)
3. Hash the `build_commands` configuration itself by serializing all repos' `build_commands` as JSON with sorted keys and hashing the result
4. This ensures changes to watch file contents OR commands list OR watch_files list trigger a rebuild

Use `get_all_build_commands()` from `config.repo_config` for step 3. If no `build_commands` are configured, skip gracefully (no hash change from baseline).

Files: `sandbox/egg_lib/docker.py`

### Phase 3: Build execution

**Goal:** Execute build commands during Docker image build.

**TASK-3-1: Add `--build-commands` flag to `docker-setup.py`**

Add to `sandbox/docker-setup.py`:

1. **CLI flag:** Check `sys.argv` for `--build-commands` (consistent with existing `main()` which doesn't use argparse)
2. **`get_build_commands()` function:** Reads `/tmp/build-commands.json` manifest, returns list of entries. Returns empty list if file missing.
3. **`install_build_commands(entries)` function:** For each entry:
   - Set working directory to `/tmp/repo-deps/<repo_name>/`
   - Create workdir if it doesn't exist
   - Run each command via `subprocess.run(cmd, shell=True, check=False, cwd=workdir, capture_output=True)`
   - Log stdout/stderr for each command
   - On failure: log prominently with command, exit code, and stderr (non-fatal)
   - After all entries: print summary listing any failed commands
4. **Dispatch in `main()`:** If `--build-commands` flag is present, call `install_build_commands(get_build_commands())` and return. Do NOT run `install_core_packages`, `install_extra_packages`, or `configure_system`.

Files: `sandbox/docker-setup.py`

**TASK-3-2: Add Dockerfile layer for repo dependency installation**

Add a new layer to `sandbox/Dockerfile` after the PyPI pre-installs section (after line 104) and before the Claude commands COPY (line 110). This is a **second invocation** of `docker-setup.py`, separate from the existing one at lines 58-63:

```dockerfile
# --- Phase: Project dependency installation (build_commands) ---
COPY sandbox/repo-deps/ /tmp/repo-deps/
COPY sandbox/build-commands.json /tmp/build-commands.json
COPY sandbox/docker-setup.py /tmp/docker-setup.py
RUN python3 /tmp/docker-setup.py --build-commands \
    && rm -f /tmp/docker-setup.py /tmp/build-commands.json
```

The existing docker-setup.py layer at lines 58-63 is NOT modified. The two layers have different cache invalidation triggers:
- OS package layer: changes only when `docker-setup.py` code or `extra_packages` change
- Build-commands layer: changes when watch files or `build_commands` config change

Files: `sandbox/Dockerfile`

### Phase 4: Tests and documentation

**Goal:** Full test coverage for all new functionality and updated docs.

**TASK-4-1: Config parsing tests**

Add `TestGetRepoBuildCommands` and `TestGetAllBuildCommands` test classes to `tests/config/test_repo_config.py`, following the existing pattern (pytest classes, `monkeypatch.setenv("EGG_REPO_CONFIG", ...)` for config injection):

- Valid config: returns correct `{"watch_files": [...], "commands": [...]}` structure
- Missing config: returns `None` for repos without `build_commands`
- Case-insensitive repo matching works
- Malformed entries: `watch_files` as string (not list), `commands` missing, wrong types -- filtered with warnings
- Empty `build_commands` section handled gracefully
- `get_all_build_commands()` collects from multiple repos correctly

Files: `tests/config/test_repo_config.py`

**TASK-4-2: Docker build context and hash tests**

Add tests to `tests/sandbox/test_docker.py` following the existing pattern (pytest classes, `unittest.mock.patch`, `MagicMock`):

**Watch file copying:**
- Watch files copied to correct paths in build context (`sandbox/repo-deps/<repo>/`)
- Missing watch files produce warnings but don't fail
- All watch files missing for a repo: repo skipped with warning
- Repo name with no local path match: skipped with warning

**Path traversal security (CRITICAL):**
- `watch_files` with `../../etc/passwd` raises `ValueError` and file is NOT in build context
- `watch_files` with `../sibling-repo/file` raises `ValueError`
- Valid relative paths within repo directory are accepted (e.g., `src/requirements.txt`)
- Paths with null bytes rejected with `ValueError`

**Manifest generation:**
- `build-commands.json` contains only `{repo_name, watch_files, commands}` fields (allowlist test)
- Manifest structure is valid JSON with expected schema

**Build hash:**
- Hash changes when watch file contents change
- Hash changes when `commands` list changes (even if watch files unchanged)
- Hash changes when `watch_files` list changes (new file added)
- Hash changes when new repo's `build_commands` added
- Hash stable when nothing changes
- Hash computation succeeds when no `build_commands` configured

Files: `tests/sandbox/test_docker.py`

**TASK-4-3: docker-setup.py execution tests**

Add tests to `tests/sandbox/test_docker_setup.py` following the existing pattern (`SourceFileLoader` import, `unittest.mock.patch` for subprocess):

- `--build-commands` flag dispatches to `install_build_commands()`, does NOT run OS package install
- No flag: existing behavior unchanged (regression test)
- `get_build_commands()` parses manifest correctly
- `get_build_commands()` returns empty list when manifest missing
- `install_build_commands()` runs commands in correct working directory
- Command failures are non-fatal: logged prominently, execution continues
- Empty manifest: no commands run (no-op)
- Summary output lists failed commands

Files: `tests/sandbox/test_docker_setup.py`

**TASK-4-4: Documentation update**

Update `sandbox/README.md` to document the `build_commands` feature:
- Configuration format (under `repo_settings` in `repositories.yaml`)
- Watch files and Docker layer caching behavior
- Working directory limitation (contains only watch files, not full repo)
- pip vs npm asymmetry: pip installs to system site-packages (works at runtime),
  npm installs to local `node_modules` (lost at runtime mount). Document `--prefer-offline`
  pattern for npm.
- Runtime dependency: runtimes (Node, Go) must be in `docker_setup.extra_packages`
- Examples for Python and Node projects

Files: `sandbox/README.md`

**TASK-4-5: Full test suite regression check**

Run `make test` (or equivalent pytest invocation) and verify:
- All existing tests still pass (no regressions)
- All new tests pass
- No unexpected warnings or errors

Files: (none -- execution only)

## Dependency Ordering

```
TASK-1-1 ─┐
           ├─→ TASK-2-1 ─→ TASK-2-2 ─→ TASK-3-1 ─→ TASK-3-2 ─→ TASK-4-1 ──┐
TASK-1-2 ─┘                                                      TASK-4-2 ──┤
                                                                  TASK-4-3 ──┼─→ TASK-4-5
                                                                  TASK-4-4 ──┘
```

- TASK-1-1 and TASK-1-2 can be done in parallel (no interdependency)
- TASK-2-1 depends on TASK-1-2 (needs `get_all_build_commands()`)
- TASK-2-2 depends on TASK-2-1 (hashes files that TASK-2-1 creates)
- TASK-3-1 depends on TASK-2-1 (reads manifest that TASK-2-1 generates)
- TASK-3-2 depends on TASK-3-1 (Dockerfile layer runs docker-setup.py --build-commands)
- TASK-4-1 through TASK-4-4 can be done in parallel after Phase 3
- TASK-4-5 depends on all other TASK-4-x tasks

## File Impact

| File | Change | Risk |
|------|--------|------|
| `config/repositories.yaml.example` | Add `build_commands` with examples | Low |
| `config/repo_config.py` | Add `get_repo_build_commands()`, `get_all_build_commands()` | Low |
| `sandbox/egg_lib/docker.py` | Extend `create_dockerfile()` (watch files + path validation + manifest) and `compute_build_hash()` | Medium |
| `sandbox/docker-setup.py` | Add `--build-commands` flag, `get_build_commands()`, `install_build_commands()` | Medium |
| `sandbox/Dockerfile` | Add COPY + RUN layer for second docker-setup.py invocation | Low |
| `tests/config/test_repo_config.py` | Add config parsing tests | Low |
| `tests/sandbox/test_docker.py` | Add watch file, path traversal, manifest, hash tests | Low |
| `tests/sandbox/test_docker_setup.py` | Add `--build-commands` dispatch and execution tests | Low |
| `sandbox/README.md` | Document `build_commands` feature | Low |

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
    the single egg image. Includes path traversal validation for watch_files
    and explicit field allowlisting for the build manifest.
phases:
  - id: 1
    name: Config layer
    goal: Add build_commands configuration schema and parsing functions
    tasks:
      - id: TASK-1-1
        description: >
          Add build_commands example to repo_settings section in
          repositories.yaml.example with watch_files (list of relative file
          paths) and commands (list of shell commands) sub-keys. Include
          commented examples for Python (pip install -r requirements.txt)
          and Node (npm ci) projects. Document caching behavior, working
          directory limitation, and runtime dependency on extra_packages.
        acceptance: >
          repositories.yaml.example contains a build_commands example under
          repo_settings with watch_files and commands sub-keys. Comments
          explain purpose, caching, working directory, and npm vs pip
          differences.
        files:
          - config/repositories.yaml.example
      - id: TASK-1-2
        description: >
          Add get_repo_build_commands(repo) and get_all_build_commands() to
          config/repo_config.py. get_repo_build_commands returns the
          build_commands dict ({watch_files, commands}) for a repo or None.
          get_all_build_commands returns a dict mapping repo names to their
          build_commands. Both use _load_config() and case-insensitive
          matching. Validate that watch_files and commands are lists of
          strings; filter invalid entries with logged warnings.
        acceptance: >
          get_repo_build_commands("org/repo") returns {watch_files: [...],
          commands: [...]} for configured repos and None for unconfigured.
          get_all_build_commands() returns all repos with valid
          build_commands. Invalid entries (wrong types, missing keys) are
          filtered out with warnings.
        files:
          - config/repo_config.py
  - id: 2
    name: Build context and hash
    goal: >
      Copy watch files into Docker build context with path traversal
      validation, generate manifest, and extend build hash
    tasks:
      - id: TASK-2-1
        description: >
          Extend create_dockerfile() in sandbox/egg_lib/docker.py to:
          (1) call get_all_build_commands() and get_local_repos(),
          (2) resolve each repo name to a local path by matching name
          after '/' against local_repos directory names,
          (3) validate all watch_files paths with Path.resolve() +
          is_relative_to(repo_base_path) -- reject null bytes and paths
          escaping the repo directory with ValueError,
          (4) copy validated watch files into sandbox/repo-deps/<repo>/,
          (5) handle missing files: warn per file, skip repo if ALL missing,
          (6) generate build-commands.json manifest with explicit field
          allowlisting ({repo_name, watch_files, commands} only),
          (7) create empty repo-deps/ and empty manifest when no
          build_commands configured so Dockerfile COPY succeeds.
          Use _copy_directory_atomic() for repo-deps/ directory.
        acceptance: >
          create_dockerfile() with build_commands copies watch files to
          build context and produces valid build-commands.json. Missing
          watch files produce warnings. Repos with no local path match
          are skipped with warning. Watch file paths that escape the repo
          directory (e.g., ../../etc/passwd) are rejected with a ValueError.
          Null byte paths rejected. Empty config produces empty but valid
          repo-deps/ and manifest.
        files:
          - sandbox/egg_lib/docker.py
      - id: TASK-2-2
        description: >
          Extend compute_build_hash() in sandbox/egg_lib/docker.py to:
          (1) hash contents of all files in the build context repo-deps/
          directory (if it exists), sorted for determinism,
          (2) hash the build_commands configuration by serializing all
          repos' build_commands as JSON with sorted keys.
          This ensures changes to watch files, commands, or watch_files
          list all trigger a rebuild.
        acceptance: >
          Build hash changes when any watch file content changes. Build
          hash changes when commands list changes. Build hash changes when
          watch_files list changes. Build hash changes when new repo added.
          Build hash stable when nothing changes. Hash computation does not
          fail when no build_commands configured.
        files:
          - sandbox/egg_lib/docker.py
  - id: 3
    name: Build execution
    goal: >
      Execute build commands during Docker image build via second
      docker-setup.py invocation
    tasks:
      - id: TASK-3-1
        description: >
          Add to sandbox/docker-setup.py:
          (1) check sys.argv for --build-commands flag,
          (2) get_build_commands() reads /tmp/build-commands.json and
          returns entries (empty list if file missing),
          (3) install_build_commands(entries) iterates entries, sets cwd
          to /tmp/repo-deps/<repo_name>/, runs each command via
          subprocess.run(shell=True, check=False) with stdout/stderr
          capture and logging -- log failures prominently with command,
          exit code, and stderr, print summary at end,
          (4) main() dispatches: --build-commands calls
          install_build_commands only; else existing OS package logic.
        acceptance: >
          docker-setup.py --build-commands reads manifest, runs commands
          in correct working directory, logs output, continues on failure.
          Missing manifest is a no-op. Without flag, existing behavior
          unchanged.
        files:
          - sandbox/docker-setup.py
      - id: TASK-3-2
        description: >
          Add a new layer to sandbox/Dockerfile after the PyPI pre-installs
          (after line 104) and before Claude commands COPY (line 110).
          This is a SECOND invocation of docker-setup.py:

          COPY sandbox/repo-deps/ /tmp/repo-deps/
          COPY sandbox/build-commands.json /tmp/build-commands.json
          COPY sandbox/docker-setup.py /tmp/docker-setup.py
          RUN python3 /tmp/docker-setup.py --build-commands
              && rm -f /tmp/docker-setup.py /tmp/build-commands.json

          The existing docker-setup.py layer at lines 58-63 is NOT
          modified. Two layers with different cache invalidation triggers.
        acceptance: >
          Dockerfile has a new layer after PyPI pre-installs that COPYs
          repo-deps/, build-commands.json, and docker-setup.py, then RUNs
          docker-setup.py --build-commands. The existing docker-setup.py
          layer is unchanged. Layer comment explains the purpose.
        files:
          - sandbox/Dockerfile
  - id: 4
    name: Tests and documentation
    goal: >
      Full test coverage for all new functionality including path
      traversal security and updated documentation
    tasks:
      - id: TASK-4-1
        description: >
          Add TestGetRepoBuildCommands and TestGetAllBuildCommands test
          classes to tests/config/test_repo_config.py following existing
          patterns (pytest classes, monkeypatch for config injection).
          Test: valid config, missing config, case-insensitive matching,
          malformed entries (wrong types, missing keys), empty
          build_commands, get_all_build_commands with multiple repos.
        acceptance: >
          Tests cover happy path, missing config, case sensitivity,
          validation, and multi-repo collection. All tests pass.
        files:
          - tests/config/test_repo_config.py
      - id: TASK-4-2
        description: >
          Add tests to tests/sandbox/test_docker.py for watch file
          copying, manifest generation, path traversal rejection, and
          build hash extension. SECURITY TESTS: verify watch_files with
          '../../etc/passwd' raises ValueError and file is NOT in build
          context; '../sibling/file' rejected; null bytes rejected; valid
          relative paths accepted. Also test: files copied to correct
          paths, manifest has allowlisted fields only, missing files
          produce warnings, hash changes on file/config changes.
        acceptance: >
          Path traversal tests verify ../../etc/passwd raises ValueError.
          Null byte paths rejected. Valid paths accepted. Manifest
          contains only allowlisted fields. Hash tests cover all change
          scenarios. All tests pass.
        files:
          - tests/sandbox/test_docker.py
      - id: TASK-4-3
        description: >
          Add tests to tests/sandbox/test_docker_setup.py for
          --build-commands flag dispatch, get_build_commands(), and
          install_build_commands(). Mock subprocess.run. Test: flag
          dispatches correctly (does NOT run OS install), manifest
          parsing, correct working directory, non-fatal failure handling
          with prominent logging, no-op on missing manifest, summary
          output for failed commands.
        acceptance: >
          Tests verify flag dispatch, manifest parsing, working directory,
          non-fatal failures, and empty/missing manifest. All tests pass.
        files:
          - tests/sandbox/test_docker_setup.py
      - id: TASK-4-4
        description: >
          Update sandbox/README.md to document build_commands feature:
          configuration format, watch files and caching behavior, working
          directory limitation, pip vs npm asymmetry with --prefer-offline
          pattern, runtime dependencies on extra_packages, and examples
          for Python and Node projects.
        acceptance: >
          README has a build_commands section with config examples,
          caching explanation, working directory notes, pip vs npm
          guidance, and runtime dependency documentation.
        files:
          - sandbox/README.md
      - id: TASK-4-5
        description: >
          Run full test suite (make test) and verify no regressions from
          existing tests and all new tests pass.
        acceptance: All existing and new tests pass with no errors.
        files: []
```

---

*Authored-by: egg*
