# Analysis: Add support for preinstalling necessary dependencies in private mode

> Issue: #32 | Phase: refine

## Problem Statement

In private mode, the egg container operates under network lockdown where only `api.anthropic.com` traffic is allowed through the Squid proxy (and even that is routed through the gateway HTTP endpoint). This means containers cannot access PyPI, npm, or any other package registries at runtime.

Currently, common Python dependencies are hardcoded in the sandbox Dockerfile (lines 75-100), but there's no mechanism for per-repository dependency customization. If a private-mode repository needs additional packages (e.g., Django for a web app, or specific testing libraries), those dependencies must be manually added to the base Dockerfile, which:

1. Requires rebuilding the egg Docker image for every new repository
2. Bloats the base image with repo-specific dependencies
3. Creates friction for onboarding new private repositories
4. Makes it difficult to maintain separate dependency sets for different projects

**Desired outcome**: A configurable, per-repository mechanism to preinstall dependencies during Docker image build, while maintaining fast container startup and Docker layer caching.

## Current Behavior

### Existing Infrastructure

The codebase already has partial infrastructure for per-repo configuration:

1. **`repositories.yaml.example`** (`config/repositories.yaml.example:134-157`) includes a `docker_setup.extra_packages` section for apt/dnf packages, but:
   - Only handles system packages (apt/dnf), not language-specific packages (pip, npm)
   - Is designed for runtime installation via `docker-setup.py`, which requires network access

2. **`docker-setup.py`** (`sandbox/docker-setup.py`) installs system packages at container runtime:
   - Reads config from `~/.config/egg/repositories.yaml` or `EGG_REPO_CONFIG` env var
   - Supports apt/dnf packages via `extra_packages.apt` and `extra_packages.dnf`
   - Runs during Docker build but has no mechanism for per-repo builds

3. **Dockerfile** (`sandbox/Dockerfile:75-100`) hardcodes common Python dependencies:
   - HTTP clients: requests, httpx, aiohttp, urllib3
   - Testing: pytest, pytest-cov, pytest-asyncio, pytest-xdist, hypothesis
   - Code quality: black, ruff, mypy, isort
   - Frameworks: flask, waitress
   - Utilities: pydantic, tenacity, cryptography, PyJWT, etc.

4. **Entrypoint** (`sandbox/entrypoint.py`) handles container startup but has no dependency installation phase (by design - network is already locked down by this point).

### Network Architecture

In private mode:
- Container is on `egg-isolated` network (172.32.0.x)
- All HTTP/HTTPS traffic routes through Squid proxy (port 3129)
- Proxy only allows `api.anthropic.com` (and even that is routed through gateway)
- PyPI, npm, GitHub raw, etc. are all blocked
- Dependencies MUST be pre-installed in the Docker image

## Constraints

### Technical Constraints

1. **Network lockdown is non-negotiable**: Private mode exists for security - sensitive repos should not have arbitrary network access
2. **Docker layer caching**: Must preserve efficient caching to keep container startup fast
3. **Multi-repo support**: A single egg installation may work with multiple repositories, each with different dependency needs
4. **Build-time vs runtime**: Dependencies must be installed at Docker BUILD time, not container START time
5. **No internet at runtime**: Any solution must work completely offline once the container starts

### Business Constraints

1. **Infrastructure agnostic**: Per the issue, solution should not be "tied to a single deployment infrastructure"
2. **Backward compatible**: Existing users should not have their workflows broken
3. **Simple onboarding**: Adding a new repo shouldn't require complex setup

### Dependencies

- Docker BuildKit for multi-stage builds and cache mounting
- Repository configuration (`repositories.yaml`)
- Existing egg image build process (`sandbox/egg_lib/docker.py`)

## Options Considered

### Option A: Per-Repository Custom Dockerfiles

**Approach**: Allow each repository to define a custom Dockerfile (e.g., `.egg/Dockerfile` or `egg.Dockerfile`) that extends the base egg image and adds repo-specific dependencies.

**Pros**:
- Maximum flexibility - repos can install anything (apt, pip, npm, custom scripts)
- Uses standard Docker patterns that developers already know
- Easy to debug - just a Dockerfile
- Full control over installation order and caching
- Works with any CI/CD system

**Cons**:
- Requires building a separate image per repository
- Duplicates base image layers if not using Docker layer caching properly
- Users must know Dockerfile syntax
- No centralized management of what's installed

**Implementation sketch**:
```yaml
# repositories.yaml
local_repos:
  paths:
    - /home/user/myproject

repo_settings:
  myorg/myproject:
    dockerfile: .egg/Dockerfile  # Or auto-detect
```

```dockerfile
# .egg/Dockerfile in the repository
FROM egg:latest

RUN pip install django djangorestframework celery
RUN npm install -g typescript
```

### Option B: Declarative Dependency Manifest

**Approach**: Extend `repositories.yaml` or add a per-repo manifest file (e.g., `.egg/dependencies.yaml`) that declares dependencies in a structured format. The egg build process reads this and installs packages.

**Pros**:
- Simple, declarative format - no Dockerfile knowledge needed
- Centralized view of dependencies in config
- Can be validated before build
- Supports multiple package managers in one config

**Cons**:
- Less flexible than Dockerfiles for complex setups
- Need to define and maintain the manifest schema
- May not cover all edge cases (custom PPAs, build dependencies, etc.)

**Implementation sketch**:
```yaml
# repositories.yaml
repo_settings:
  myorg/myproject:
    dependencies:
      pip:
        - django>=4.0
        - djangorestframework
        - celery[redis]
      npm:
        - typescript
        - eslint
      apt:
        - libpq-dev
        - redis-tools
```

Or as a separate file in the repository:
```yaml
# .egg/dependencies.yaml
pip:
  - django>=4.0
  - djangorestframework
  - celery[redis]
npm:
  - typescript
apt:
  - libpq-dev
```

### Option C: Build-Time Hook Script

**Approach**: Allow repos to define a setup script (e.g., `.egg/setup.sh`) that runs during Docker image build. This script has full network access at build time and can install anything.

**Pros**:
- Full flexibility via shell scripting
- Can handle complex multi-step setups
- Familiar to developers (just a shell script)
- Can source existing setup scripts (e.g., `make deps`)

**Cons**:
- Security risk - arbitrary code execution during build
- Harder to cache effectively (script changes invalidate entire layer)
- Debugging build failures can be tricky
- Reproducibility depends on script quality

**Implementation sketch**:
```yaml
# repositories.yaml
repo_settings:
  myorg/myproject:
    setup_script: .egg/setup.sh  # Runs during docker build
```

```bash
#!/bin/bash
# .egg/setup.sh
pip install -r requirements.txt
npm ci
apt-get install -y postgresql-client
```

### Option D: Hybrid Approach (Declarative + Escape Hatch)

**Approach**: Combine Option B's declarative manifest for common cases with Option C's script hook for edge cases. The declarative deps are installed first (enabling caching), then the optional script runs.

**Pros**:
- Best of both worlds - simple cases stay simple, complex cases are possible
- Good caching for declarative deps
- Script runs in known state (after declared deps installed)
- Can migrate from script to declarative over time

**Cons**:
- More complex implementation
- Two ways to do things could cause confusion
- Script still has security/caching concerns

**Implementation sketch**:
```yaml
# .egg/dependencies.yaml
pip:
  - django>=4.0
  - djangorestframework
npm:
  - typescript
# Optional: runs after declarative deps
setup_script: setup.sh
```

## Recommended Approach

**Option D: Hybrid Approach (Declarative + Escape Hatch)**

This recommendation is based on several factors:

1. **Most repos have simple needs**: A declarative manifest covers 90% of use cases (pip packages, npm packages, apt packages). Making the common case simple reduces friction.

2. **Escape hatch prevents blocking**: The 10% of repos with complex needs (custom PPAs, build-from-source, environment setup) can use the script hook without being blocked.

3. **Caching optimized for common case**: Declarative deps can be installed in a well-structured Docker layer that caches efficiently. Only changes to declared deps bust the cache.

4. **Infrastructure agnostic**: The manifest format and script execution are standard across deployment environments. No dependency on specific CI/CD systems.

5. **Progressive complexity**: New users can start with just `pip:` entries. Advanced users can add scripts when needed. No need to learn everything upfront.

### Recommended Implementation Details

1. **Config location**: `.egg/dependencies.yaml` in the repository root (keeps egg config together)

2. **Supported package managers**: `pip`, `npm`, `apt`, `dnf` initially. Can expand later.

3. **Build process**:
   - Read manifest at `docker build` time
   - Generate a temporary requirements.txt / package.json from manifest
   - Run installers with `--no-cache-dir` / `--production` flags
   - Optionally run setup script

4. **Caching strategy**:
   - COPY only the manifest file first (new layer)
   - Install deps (cached until manifest changes)
   - COPY rest of repo (separate layer)

5. **Multi-repo handling**: Build a custom image per-repo using the base egg image + repo deps

## Open Questions

### HITL Decision Required

```bash
egg-contract add-decision --question "Which configuration location should we use for per-repository dependencies?" \
  --options "In-repo file (.egg/dependencies.yaml)" "Central config (repositories.yaml)" "Both (with precedence rules)" --format markdown
```

- [ ] In-repo file (`.egg/dependencies.yaml`)
  - Keeps config with the code it describes
  - Version controlled with the repo
  - No central configuration needed
- [ ] Central config (`repositories.yaml`)
  - Single place to see all repo configurations
  - No changes needed to target repositories
  - Easier to manage many repos
- [ ] Both (with precedence rules)
  - Maximum flexibility
  - In-repo overrides central config
  - More complexity to document and maintain
- [ ] Other (explain in reply)

### Open-Ended Questions

1. **Should we support requirements.txt/package.json directly?**
   Many repos already have `requirements.txt` or `package.json`. Should we support auto-detecting and using these files directly, or require the `.egg/dependencies.yaml` format for explicit control?

2. **What happens when dependency installation fails?**
   Should the build fail immediately, or should we have a "soft fail" mode that logs warnings but continues? Failed builds mean users can't start work at all.

3. **Should we support pinned versions vs version ranges?**
   Exact pinning (e.g., `django==4.2.1`) provides reproducibility but requires maintenance. Version ranges (e.g., `django>=4.0,<5.0`) are more flexible but could cause inconsistencies. Should we mandate one or support both?

---

*Authored-by: egg*
