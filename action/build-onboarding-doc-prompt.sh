#!/usr/bin/env bash
# build-onboarding-doc-prompt.sh — Build a prompt for full-repo documentation onboarding
#
# This script creates a comprehensive prompt that instructs Claude to iterate
# through an entire repository and produce thorough, indexed documentation —
# following the same structure that the incremental doc-updater maintains.
#
# Unlike the doc-updater (which reacts to individual commits), this is a
# one-time or periodic "bootstrap" that documents an entire codebase from
# scratch, incorporating any existing documentation.
#
# Environment variables:
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#   DRY_RUN            — (Optional) If "true", analyze only, don't create PR
#   INCLUDE_PATTERN    — (Optional) Glob pattern to limit scope (e.g. "gateway/**")
#   EXCLUDE_DIRS       — (Optional) Comma-separated dirs to skip (added to defaults)
#
# Output:
#   Sets 'prompt_file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

# Directories to always exclude from scanning
DEFAULT_EXCLUDE="node_modules,.git,__pycache__,dist,build,.egg-state,venv,.venv,.mypy_cache,.pytest_cache,.ruff_cache,.tox,vendor,coverage,.next,.nuxt"

get_exclude_dirs() {
    # Yields validated directory names from DEFAULT_EXCLUDE + EXCLUDE_DIRS, one per line.
    # Trims whitespace and rejects names that don't match the whitelist pattern.
    IFS=',' read -ra DIRS <<< "${DEFAULT_EXCLUDE},${EXCLUDE_DIRS:-}"
    for dir in "${DIRS[@]}"; do
        dir="${dir#"${dir%%[![:space:]]*}"}"   # trim leading whitespace
        dir="${dir%"${dir##*[![:space:]]}"}"   # trim trailing whitespace
        if [[ -n "$dir" ]]; then
            if [[ "$dir" =~ ^[a-zA-Z0-9._-]+(/[a-zA-Z0-9._-]+)*$ ]]; then
                echo "$dir"
            else
                echo "Warning: skipping invalid exclude dir: $dir" >&2
            fi
        fi
    done
}

build_find_excludes() {
    # Build an array of -not -path exclusion args for use with find starting from ".".
    # Results stored in FIND_EXCLUDES array.
    FIND_EXCLUDES=()
    local dir
    while IFS= read -r dir; do
        FIND_EXCLUDES+=("-not" "-path" "./${dir}/*")
    done < <(get_exclude_dirs)
}

get_directory_tree() {
    local depth="${1:-3}"
    local -a find_args=("." "-maxdepth" "$depth")

    local dir
    while IFS= read -r dir; do
        find_args+=("-path" "./${dir}" "-prune" "-o")
    done < <(get_exclude_dirs)

    find_args+=("-type" "d" "-print")
    find "${find_args[@]}" 2>/dev/null | sort | head -200
}

get_top_level_contents() {
    # List top-level files and directories with brief descriptions
    ls -1p 2>/dev/null | head -50
}

detect_languages() {
    # Detect primary languages by file extension count
    build_find_excludes
    find . -type f "${FIND_EXCLUDES[@]}" 2>/dev/null | \
    sed -n 's|.*/[^/]*\.\([^./]*\)$|\1|p' | sort | uniq -c | sort -rn | head -15
}

find_existing_docs() {
    # Find all markdown files that could be documentation
    build_find_excludes
    find . -name "*.md" -type f \
        "${FIND_EXCLUDES[@]}" \
        -not -path './.egg/*' \
        -not -path './CHANGELOG.md' \
        2>/dev/null | sort
}

find_readmes() {
    # Find all README files specifically
    build_find_excludes
    find . -iname "README*" -type f \
        "${FIND_EXCLUDES[@]}" \
        2>/dev/null | sort
}

find_config_files() {
    # Find configuration/build files that reveal project structure
    build_find_excludes
    find . -maxdepth 2 \( \
        -name "Makefile" -o \
        -name "Dockerfile" -o \
        -name "docker-compose*.yml" -o \
        -name "pyproject.toml" -o \
        -name "setup.py" -o \
        -name "setup.cfg" -o \
        -name "package.json" -o \
        -name "tsconfig.json" -o \
        -name "go.mod" -o \
        -name "Cargo.toml" -o \
        -name "pom.xml" -o \
        -name "build.gradle" -o \
        -name ".eslintrc*" -o \
        -name "jest.config*" -o \
        -name "pytest.ini" -o \
        -name "tox.ini" \
    \) -type f "${FIND_EXCLUDES[@]}" 2>/dev/null | sort
}

count_source_files() {
    # Count files per top-level directory to show project scale.
    # Uses -name based pruning so exclusions work regardless of find start path.
    local -a prune_args=()
    local dir
    local first=true
    while IFS= read -r dir; do
        local basename="${dir##*/}"
        if [[ "$first" == true ]]; then
            first=false
        else
            prune_args+=("-o")
        fi
        prune_args+=("-name" "$basename")
    done < <(get_exclude_dirs)

    for dir in */; do
        [[ -d "$dir" ]] || continue
        local count
        if [[ ${#prune_args[@]} -gt 0 ]]; then
            count=$(find "$dir" \( "${prune_args[@]}" \) -prune -o -type f -print 2>/dev/null | wc -l)
        else
            count=$(find "$dir" -type f 2>/dev/null | wc -l)
        fi
        echo "  ${dir} — ${count} files"
    done 2>/dev/null
}

detect_entry_points() {
    # Find likely entry points: main files, CLI scripts, app entrypoints
    build_find_excludes
    find . -maxdepth 3 \( \
        -name "main.py" -o \
        -name "app.py" -o \
        -name "cli.py" -o \
        -name "server.py" -o \
        -name "index.ts" -o \
        -name "index.js" -o \
        -name "main.go" -o \
        -name "main.rs" -o \
        -name "entrypoint.sh" -o \
        -name "manage.py" \
    \) -type f \
        "${FIND_EXCLUDES[@]}" \
        2>/dev/null | sort
}

# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

build_prompt() {
    local directory_tree
    local top_level
    local languages
    local existing_docs
    local readmes
    local config_files
    local file_counts
    local entry_points

    directory_tree=$(get_directory_tree)
    top_level=$(get_top_level_contents)
    languages=$(detect_languages)
    existing_docs=$(find_existing_docs)
    readmes=$(find_readmes)
    config_files=$(find_config_files)
    file_counts=$(count_source_files)
    entry_points=$(detect_entry_points)

    local include_note=""
    if [[ -n "${INCLUDE_PATTERN:-}" ]]; then
        include_note="
> **Scope limited to:** \`${INCLUDE_PATTERN}\`. Only document files and
> directories matching this pattern. Still create an index covering the
> scoped area."
    fi

    local prompt
    prompt=$(cat <<PROMPT_EOF
# Documentation Onboarding Task

Generate comprehensive, well-indexed documentation for this entire repository.
Produce the same structured output that the incremental doc-updater maintains,
but built from scratch by surveying the full codebase.
${include_note}

## Repository Snapshot

**Repository:** ${GITHUB_REPOSITORY}

### Top-level contents
\`\`\`
${top_level}
\`\`\`

### Directory tree (depth 3)
\`\`\`
${directory_tree}
\`\`\`

### File counts per top-level directory
\`\`\`
${file_counts}
\`\`\`

### Language distribution (by file extension)
\`\`\`
${languages}
\`\`\`

### Configuration / build files
\`\`\`
${config_files:-none found}
\`\`\`

### Entry points detected
\`\`\`
${entry_points:-none found}
\`\`\`

### Existing documentation files
\`\`\`
${existing_docs:-none found}
\`\`\`

### Existing README files
\`\`\`
${readmes:-none found}
\`\`\`

## Your Task — Full Documentation Onboarding

Work through the repository systematically, directory by directory, and produce
a complete documentation set. Follow this process:

### Phase 1: Deep Survey

Before writing any documentation, thoroughly read and understand the codebase.
For each top-level directory:

1. Read the directory's existing README (if any) to understand stated purpose.
2. Read key source files (entry points, main modules, public APIs).
3. Identify the component's responsibilities, interfaces, and dependencies.
4. Note any configuration, environment variables, or CLI flags.
5. Identify test infrastructure (frameworks, config, how to run tests).

Also examine:
- CI/CD configuration (GitHub Actions, Makefile targets)
- Docker setup (Dockerfiles, compose files)
- Dependency manifests (package.json, pyproject.toml, go.mod, etc.)
- Any existing ADRs, guides, or design documents

**Context budget:** Don't read every file — focus on entry points, public
interfaces, configuration, and existing documentation. Skip generated files,
lock files, and vendor directories.

### Phase 2: Documentation Plan

After surveying, decide on the documentation structure. Use this template as
your target layout, adapting to what the repo actually contains:

\`\`\`
docs/
├── index.md                      # Master navigation index (REQUIRED)
├── architecture/
│   └── README.md                 # System design, components, data flow
├── development/
│   └── STRUCTURE.md              # Directory layout and conventions
├── guides/
│   ├── quickstart.md             # Getting started (setup, first run)
│   ├── deployment.md             # Deployment options and configuration
│   └── <topic>.md                # Additional guides as needed
├── adr/
│   └── README.md                 # ADR index (if ADRs exist)
└── <other sections as needed>

<component>/README.md             # Per-component README (in component dir)
\`\`\`

**Key principles for the plan:**
- Don't create docs for the sake of it. Only document what provides value.
- Consolidate thin topics into broader documents rather than many tiny files.
- If the repo already has good documentation, update and index it rather than
  rewriting from scratch.
- Component READMEs live inside their component directory (not in docs/).

### Phase 3: Write Documentation

Create each document, following these guidelines:

#### docs/index.md (REQUIRED — create first)
The master navigation hub. Structure it like this:

\`\`\`markdown
# Documentation Index

> <One-line project description>

This index helps both humans and LLMs navigate the documentation efficiently.

## Core Documentation

### <Section>
| Document | Description |
|----------|-------------|
| [Name](path) | One-line description |

## Component Documentation
| Component | Location | Description |
|-----------|----------|-------------|
| [Name](path) | \`dir/\` | One-line description |

## Task-Specific Guides
| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **Task** | [Doc](path) | [Doc](path) |

## Quick Navigation
**Getting Started:** ...
**Understanding the System:** ...

---
*Last updated: <date>*
\`\`\`

#### docs/development/STRUCTURE.md
Document every top-level directory and its contents:
- Purpose and responsibilities
- Key files and what they do
- Internal structure (sub-directories)
- Which environment it runs in (if applicable)

#### docs/architecture/README.md
Cover:
- System overview and high-level design
- Component relationships and data flow
- Key design principles / patterns
- Security model (if applicable)
- External dependencies and integrations

#### Component READMEs (<component>/README.md)
For each major component directory, create or update a README covering:
- What the component does
- Key files and their roles
- How to develop / test the component
- Configuration and environment variables
- Interfaces with other components

#### Guides (docs/guides/*.md)
Create guides for practical tasks:
- **quickstart.md**: Clone, install deps, run locally, run tests
- **deployment.md**: How to deploy (if applicable)
- Additional guides for complex workflows

#### ADRs (docs/adr/)
If the repo has existing ADRs, create an index. Don't invent new ADRs.

### Phase 4: Incorporate Existing Documentation

This is critical — don't throw away existing docs:

1. **Existing READMEs**: Read them. If accurate, keep them and link from the
   index. If outdated, update them in place. If they overlap with new docs,
   consolidate (prefer the component README as the primary source).

2. **Existing docs/ directory**: If one exists, integrate new documentation
   alongside existing files. Update the index to cover everything.

3. **Inline documentation**: If the repo has good code comments, don't
   duplicate that information in docs. Reference it instead.

4. **Existing ADRs**: Index them. Don't modify their content (ADRs are
   immutable records of decisions).

### Phase 5: Cross-Reference and Validate

1. Verify every link in index.md points to a real file.
2. Ensure every doc file is reachable from index.md.
3. Check that component READMEs reference architecture docs where relevant.
4. Verify the STRUCTURE.md matches the actual directory layout.

## Writing Style Guidelines

- **Be concise.** One clear sentence beats three vague ones.
- **Focus on "what" and "why"**, not implementation details.
- **Use tables** for structured information (components, files, configs).
- **Use code blocks** for commands, file paths, and configuration examples.
- **Write for someone new to the repo** — explain context, don't assume
  familiarity.
- **Match existing style** if the repo already has documentation. Don't impose
  a different voice or format.
- **No marketing language.** Describe what the software does factually.

## Output

When complete:

1. Create a branch: \`egg/onboarding-docs\`
2. Commit all documentation changes
3. Create a PR with:
   - Title: \`docs: Add comprehensive documentation [doc-updater]\`
   - Body explaining the documentation structure created
   - List of all files created/updated

If this is a large repository, prioritize:
1. index.md (always)
2. STRUCTURE.md (always)
3. architecture/README.md (always)
4. Component READMEs (for major components)
5. Quickstart guide (if setup steps exist)
6. Additional guides (if time/context permits)

## Important Notes

- **Do NOT modify source code.** This is a documentation-only task.
- **Do NOT delete existing documentation** unless it's clearly wrong or
  superseded. When in doubt, keep it and link to it.
- **Do NOT document generated files**, lock files, or build artifacts.
- **Do NOT create placeholder docs** ("TODO: document this"). Either document
  it or skip it.
- Read files before documenting them — don't guess what code does from
  filenames alone.
PROMPT_EOF
)

    # Add dry run instruction if applicable
    if [[ "${DRY_RUN:-false}" == "true" ]]; then
        prompt+="

## Dry Run Mode

This is a dry run. Survey the repository and describe what documentation you
WOULD create, including the proposed structure and a summary of each document's
contents. Do NOT create any branches, commits, or PRs."
    fi

    # Write prompt to temp file
    local prompt_file="${RUNNER_TEMP:-/tmp}/onboarding-doc-prompt.txt"
    printf '%s\n' "$prompt" > "$prompt_file"

    # Use sonnet for the main work (good balance of capability and speed).
    # For very large repos, consider opus for higher quality.
    local model="sonnet"

    # Write outputs
    {
        echo "prompt_file=${prompt_file}"
        echo "model=${model}"
    } >> "${GITHUB_OUTPUT:-/dev/null}"

    echo "Onboarding doc prompt built: ${#prompt} chars, model=${model}"
    echo "Existing docs found: $(echo "$existing_docs" | grep -c '.' || true) files"
    echo "Existing READMEs: $(echo "$readmes" | grep -c '.' || true) files"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

build_prompt
