# Implementation Plan: Build-time dependency installation via build_commands

> Issue: #904 | Phase: plan | Pipeline: issue-904 | Revision: 2

## Summary

In private mode, sandbox containers have no network access beyond the Anthropic API,
so runtime dependency installation (`pip install`, `npm ci`, etc.) fails. This plan
implements build-time dependency installation by adding a `build_commands` section to
per-repo settings in `repositories.yaml`. Watch files (lockfiles, requirements.txt)
are copied into the Docker build context to enable layer caching, and user-defined
commands run during image build to bake dependencies into the single `egg` image.

## Revision 2 Changes

This revision addresses two blocking review items:

1. **Path traversal validation (TASK-2-1)**: All `watch_files` paths are now validated
   with `Path.resolve()` + `is_relative_to(repo_base_path)` before copying. Paths that
   escape the repo directory (e.g., `../../etc/passwd`) are rejected with a clear error.
   Corresponding test case added to TASK-4-2. See DD-8 in architect output.

2. **Dockerfile approach committed (TASK-3-2)**: Resolved ambiguity by committing to
   option (a) — a **second RUN of docker-setup.py with `--build-commands` flag** in a
   new Dockerfile layer. The existing docker-setup.py layer (lines 58-63, OS packages)
   is completely unchanged, preserving its cache. See DD-7 in architect output for
   Dockerfile pseudo-code.

## Approach

**Single PR with four implementation phases** within one commit stream:

1. **Config layer** — Add `build_commands` schema to `repositories.yaml.example` and
   parsing functions to `config/repo_config.py`. This is the data foundation.
2. **Build context and hash** — Extend `create_dockerfile()` to copy watch files
   (with path traversal validation) and generate a `build-commands.json` manifest in
   the build context. Extend `compute_build_hash()` to include watch file contents and
   build commands config.
3. **Build execution** — Add `--build-commands` flag to `docker-setup.py` to read the
   manifest and execute build commands. Add the corresponding Dockerfile layer as a
   second invocation of docker-setup.py.
4. **Tests and docs** — Add unit tests for all new functions (including path traversal
   rejection) and update documentation.

Each phase builds on the prior one. Repos without `build_commands` are completely
unaffected (backwards compatible).

## Key Design Decisions

Per the architect's analysis (DD-1 through DD-8):

- **Repo-to-path mapping** (DD-1): Match the repo name segment after `/` against
  `local_repos.paths` directory names. Skip with a warning if no match.
- **Dependency artifacts go to system paths** (DD-2): `pip install` goes to system
  site-packages. `npm ci` warms the npm cache (node_modules in the build dir won't
  persist, but cached packages make runtime install near-instant via `--prefer-offline`).
- **Non-fatal failures** (DD-3): Build command failures are logged prominently (with
  command, exit code, stderr) but don't abort the image build. Summary printed at end.
- **Dockerfile layer placement** (DD-4): The new layer goes after all toolchain
  installations (Python deps, pip packages) but before the frequently-changing
  `COPY . /opt/egg-runtime/` layer, maximizing cache reuse.
- **Build-commands.json manifest** (DD-6): `create_dockerfile()` generates a minimal
  JSON manifest containing only build-relevant data ({repo_name, watch_files, commands}).
  Uses explicit field allowlisting to prevent sensitive data leakage. Manifest is
  deleted after the build layer completes.
- **Second docker-setup.py invocation** (DD-7): The new Dockerfile layer COPYs
  docker-setup.py again and runs it with `--build-commands`. The existing invocation
  (line 58-63, OS packages) is unchanged. Two separate cache layers with different
  invalidation triggers.
- **Path traversal validation** (DD-8): All watch_files paths are validated with
  `Path.resolve()` + `is_relative_to()` before copying. Paths escaping the repo
  directory are rejected with a clear error.

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

| Risk | Mitigation |
|------|------------|
| **Path traversal via watch_files (R-6)** | **MITIGATED**: Path.resolve() + is_relative_to() validation. Paths escaping repo directory rejected with error. Test case in TASK-4-2. |
| Sensitive data in manifest (R-3) | Explicit field allowlisting in manifest generation. Only {repo_name, watch_files, commands}. |
| Build commands fail in watch-file-only dir | Document that working directory contains only watch_files. Provide examples. |
| npm artifacts lost at runtime (R-1) | Document pip vs npm asymmetry. Document --prefer-offline pattern for npm. |
| Runtimes not installed for build commands | Layer placement ensures Python available. Document Node/Go must be in extra_packages. |
| Config not available during build | Build-commands.json manifest approach (DD-6). |
| Stale deps | Build hash includes watch file contents — auto-detected. |
| Silent build command failures (R-5) | Prominent logging with command, exit code, stderr. Summary at end. |

## Test Strategy

1. **Config tests** (`tests/config/test_repo_config.py`):
   - `get_repo_build_commands()` returns correct structure for valid config
   - Returns `None`/empty for repos without `build_commands`
   - Case-insensitive repo matching works
   - Malformed entries (missing keys, wrong types) handled gracefully

2. **Docker build context tests** (`tests/sandbox/test_docker.py`):
   - Watch files copied to correct paths in build context
   - `build-commands.json` manifest generated with correct structure (allowlisted fields only)
   - Missing watch files produce warnings but don't fail
   - **Path traversal: watch_files with `../../etc/passwd` rejected with error**
   - **Path traversal: valid relative paths within repo directory accepted**
   - Build hash changes when watch files change
   - Build hash changes when build_commands config changes
   - Build hash stable when nothing changes

3. **Build execution tests** (`tests/sandbox/test_docker_setup.py`):
   - `--build-commands` flag dispatches to install_build_commands(), NOT OS package install
   - No flag: existing behavior unchanged
   - `get_build_commands()` parses manifest correctly
   - `install_build_commands()` runs commands in correct directory
   - Command failures are non-fatal (logged prominently, not raised)
   - Empty/missing manifest means no commands run (no-op)

4. **Existing tests**: Run full test suite to verify no regressions.

## File Impact

| File | Change | Risk |
|------|--------|------|
| `config/repositories.yaml.example` | Add `build_commands` with `watch_files` and `commands` | Low |
| `config/repo_config.py` | Add `get_repo_build_commands()` and `get_all_build_commands()` | Low |
| `sandbox/egg_lib/docker.py` | Extend `create_dockerfile()` (watch files + path validation + manifest) and `compute_build_hash()` | Medium |
| `sandbox/docker-setup.py` | Add `--build-commands` flag, `get_build_commands()`, `install_build_commands()` | Medium |
| `sandbox/Dockerfile` | Add COPY + RUN layer for second docker-setup.py invocation | Low |
| `tests/config/test_repo_config.py` | Add config parsing tests | Low |
| `tests/sandbox/test_docker.py` | Add watch file, path traversal, manifest, hash tests | Low |
| `tests/sandbox/test_docker_setup.py` | Add --build-commands dispatch and execution tests | Low |
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
    goal: Copy watch files into build context (with path traversal validation), generate manifest, and extend build hash
    tasks:
      - id: TASK-2-1
        description: "Extend create_dockerfile() in sandbox/egg_lib/docker.py to: (1) import and call get_all_build_commands() from config/repo_config.py, (2) resolve each repo name to a local path by matching the name segment after '/' against get_local_repos() directory names, (3) validate that all watch_files paths, after resolution against the repo's local directory, remain within that directory — use Path.resolve() and check is_relative_to(repo_base_path), reject paths that escape with a clear error, (4) for each matched repo, copy its validated watch_files from the local repo path into ~/.cache/egg/repo-deps/<repo-name>/, (5) generate a build-commands.json manifest in ~/.cache/egg/sandbox/ containing only {repo_name, commands, workdir} for each repo (explicit field allowlisting)"
        acceptance: "Running create_dockerfile() with build_commands configured copies watch files to the build context and produces a valid build-commands.json manifest. Missing watch files produce warnings. Repos with no local path match are skipped. Watch file paths that escape the repo directory (e.g., ../../etc/passwd) are rejected with an error."
        files:
          - sandbox/egg_lib/docker.py
      - id: TASK-2-2
        description: "Extend compute_build_hash() in sandbox/egg_lib/docker.py to: (1) hash the contents of all watch files in the build context repo-deps/ directory (if it exists), (2) hash the build_commands configuration itself (serialized to a stable JSON string with sorted keys). This ensures changes to watch files or commands trigger a rebuild."
        acceptance: Build hash changes when any watch file content changes. Build hash changes when build_commands config changes (added/removed/modified commands). Build hash is stable when nothing changes. Hash computation does not fail when no build_commands are configured.
        files:
          - sandbox/egg_lib/docker.py
  - id: 3
    name: Build execution
    goal: Execute build commands during Docker image build via second docker-setup.py invocation
    tasks:
      - id: TASK-3-1
        description: "Add to sandbox/docker-setup.py: (1) --build-commands CLI flag via argparse or sys.argv check, (2) get_build_commands() function that reads /tmp/build-commands.json manifest and returns entries, (3) install_build_commands(entries) function that iterates entries, creates workdir if needed, cd's to it, and runs each command via subprocess.run(shell=True, check=False) with stdout/stderr logging — log failures prominently with command, exit code, and stderr, print summary at end, (4) main() dispatches: if --build-commands → call install_build_commands only; else → existing OS package logic"
        acceptance: "docker-setup.py --build-commands reads the manifest, runs each command in the correct working directory, logs output, and continues on failure (non-fatal). When no manifest exists, the function is a no-op. Without the flag, existing behavior is unchanged."
        files:
          - sandbox/docker-setup.py
      - id: TASK-3-2
        description: "Add a new layer to sandbox/Dockerfile after the PyPI pre-installs section (after line 104) and before the Claude commands COPY (line 110). This is a SECOND invocation of docker-setup.py, separate from the existing one at lines 58-63. The exact Dockerfile addition:\n\n```dockerfile\n# --- Phase: Project dependency installation (build_commands) ---\nCOPY sandbox/repo-deps/ /tmp/repo-deps/\nCOPY sandbox/build-commands.json /tmp/build-commands.json\nCOPY sandbox/docker-setup.py /tmp/docker-setup.py\nRUN python3 /tmp/docker-setup.py --build-commands \\\n    && rm -f /tmp/docker-setup.py /tmp/build-commands.json\n```\n\nThe existing docker-setup.py layer at lines 58-63 is NOT modified. The two layers have different cache invalidation triggers: the OS package layer changes only when docker-setup.py code or extra_packages change, while the build-commands layer changes when watch files or build_commands change."
        acceptance: Dockerfile has a new layer that COPYs repo-deps/, build-commands.json, and docker-setup.py, then RUNs docker-setup.py --build-commands. Docker layer caching works — changing a watch file rebuilds the dependency layer but not earlier layers. The existing docker-setup.py layer is unchanged.
        files:
          - sandbox/Dockerfile
  - id: 4
    name: Tests and documentation
    goal: Add test coverage for all new functionality (including security) and update documentation
    tasks:
      - id: TASK-4-1
        description: Add TestGetRepoBuildCommands and TestGetAllBuildCommands test classes to tests/config/test_repo_config.py. Test valid config, missing config, case-insensitive matching, malformed entries (wrong types, missing keys), and empty build_commands.
        acceptance: Tests cover happy path, missing config, case sensitivity, and validation. All tests pass.
        files:
          - tests/config/test_repo_config.py
      - id: TASK-4-2
        description: "Add tests to tests/sandbox/test_docker.py for watch file copying in create_dockerfile() and build hash extension. Test that watch files are copied to correct build context paths, manifest is generated correctly (allowlisted fields only), missing files produce warnings, and build hash includes watch file contents and build_commands config. SECURITY TEST: verify that watch_files with path traversal attempts (e.g., '../../etc/passwd', '../sibling/file') are rejected with an error and the file is NOT copied to the build context."
        acceptance: "Tests verify file copying, manifest structure, graceful handling of missing files, hash sensitivity to watch files and config changes. Path traversal test: watch_files with '../../etc/passwd' raises ValueError and the file is not in the build context."
        files:
          - tests/sandbox/test_docker.py
      - id: TASK-4-3
        description: "Add tests to tests/sandbox/test_docker_setup.py for --build-commands flag dispatch, get_build_commands(), and install_build_commands(). Mock subprocess.run. Test: (1) --build-commands dispatches correctly (does NOT run OS package install), (2) manifest parsing, (3) command execution in correct directory, (4) non-fatal failure handling with prominent logging, (5) no-op when manifest is missing."
        acceptance: Tests verify flag dispatch, manifest parsing, correct working directory, non-fatal failures, and empty/missing manifest handling. All tests pass.
        files:
          - tests/sandbox/test_docker_setup.py
      - id: TASK-4-4
        description: Update sandbox/README.md to document the build_commands feature — configuration format, caching behavior, limitations (watch-file-only working directory), pip vs npm asymmetry, and examples for Python and Node projects.
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
