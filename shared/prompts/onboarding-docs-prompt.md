<!-- Shared onboarding-docs prompt: consumed by egg-onboarding-docs CLI.
     Defines the documentation standard for onboarding a repository into
     index-based, navigable documentation. -->

# Documentation Onboarding Standard

You are onboarding this repository into a structured, navigable documentation system.
Your goal: create documentation that both humans and LLM agents can efficiently navigate.

## Design Philosophy

### Index, Don't Dump

Documentation is organized as a **navigation hierarchy**, not a knowledge dump.
`docs/index.md` is the single entry point — a table of contents that links to
everything. A reader (human or agent) should be able to find any topic by scanning
the index, not by guessing file names or grepping.

### Pull, Don't Push

Documentation lives **close to the code it describes**. Each major component gets a
README in its own directory. The index *points to* these READMEs rather than
duplicating their content. This keeps docs accurate as code evolves — the doc-updater
workflow maintains the structure you create here.

### Write for Navigation, Not Reading

Most readers won't read docs linearly. They arrive with a question ("how do I
configure X?") and need to find the answer fast. Use tables, short paragraphs,
and cross-references. Avoid long prose.

## Target Documentation Structure

Create the following structure, adapting to the repository's actual components:

```
docs/
├── index.md                      # Master navigation hub (REQUIRED)
├── architecture/
│   └── README.md                 # System design, components, data flow
├── development/
│   └── STRUCTURE.md              # Directory layout and conventions
├── guides/
│   ├── quickstart.md             # Getting started for new developers
│   └── <topic>.md                # Additional guides as needed
└── adr/
    └── README.md                 # ADR index (if ADRs exist)

<component>/README.md             # Per-component READMEs (alongside code)
CONTRIBUTING.md                   # Development workflow (repo root)
```

Not every file is needed for every repository. A small library may only need
`docs/index.md`, a root `README.md`, and `CONTRIBUTING.md`. Use judgment.

## docs/index.md Specification

This is the most important file. It must serve as a complete navigation hub.

### Required Sections

**1. Header** — One-line project description as a blockquote.

**2. Core Documentation** — Table linking to architecture docs, development docs, and guides:

```markdown
## Core Documentation

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture/README.md) | System design and component interactions |
| [Project Structure](development/STRUCTURE.md) | Directory layout and conventions |
| [Contributing](../CONTRIBUTING.md) | Development setup, workflow, PR process |
```

**3. Component Documentation** — Table linking to per-component READMEs:

```markdown
## Component Documentation

| Component | Location | Description |
|-----------|----------|-------------|
| [API Server](../src/api/README.md) | `src/api/` | REST API endpoints and middleware |
| [Database](../src/db/README.md) | `src/db/` | Schema, migrations, query patterns |
```

**4. Task-Specific Guides** — Table mapping common tasks to the docs to read first:

```markdown
## Task-Specific Guides

| Task Type | Read First | Also Helpful |
|-----------|------------|--------------|
| **API changes** | [API README](../src/api/README.md) | [Architecture](architecture/README.md) |
| **Adding tests** | [Contributing](../CONTRIBUTING.md) | [Project Structure](development/STRUCTURE.md) |
```

This table is critical for agents — it answers "I need to do X, where do I start?"

**5. Quick Navigation** — Numbered list for the most common entry paths (getting started, understanding the system).

### Formatting Rules

- Use relative links (not absolute URLs)
- Link to the actual file, not a directory
- Every link must be valid — verify paths exist before referencing them
- Keep descriptions to one line per table cell

## Component README Specification

Each major directory (identifiable by having its own purpose, configuration, or entry
points) gets a README.md with:

1. **Overview** — What this component does (2-3 sentences max)
2. **Architecture** — How it fits into the larger system. Mention key dependencies and
   what depends on it.
3. **Key Concepts** — Domain terms, important abstractions, or design patterns used
4. **Configuration** — Environment variables, config files, feature flags
5. **Testing** — How to run this component's tests
6. **Related Docs** — Links back to the index and to related components

Keep READMEs under 200 lines. If a component needs more, split into sub-docs and
link from the README.

## CONTRIBUTING.md Specification

If one doesn't exist, create it at the repository root. Include:

1. **Development Setup** — Prerequisites, clone, install, first run
2. **Development Workflow** — Branch strategy, commit conventions, how to run locally
3. **Code Style** — Language-specific conventions, linter configuration
4. **Testing** — How to run tests, how to write new tests, coverage expectations
5. **PR Process** — What reviewers look for, how to get reviews

## Working with Existing Documentation

- **Incorporate, don't replace.** If docs already exist, reorganize them into this
  structure. Preserve content and authorship.
- **ADRs are immutable.** Never modify existing Architecture Decision Records. You may
  create an ADR index (`docs/adr/README.md`) that links to them.
- **README.md at root stays.** The root README is the public face of the project. Update
  it to link to `docs/index.md` for detailed documentation, but don't gut it.
- **Respect .egg/ overrides.** If `.egg/onboarding-rules.md` exists in the target
  repository, follow its instructions — they take precedence over this standard.

## Writing Style

- **Imperative voice** for instructions ("Run `make test`", not "You should run...")
- **Present tense** for descriptions ("The API server handles...", not "will handle")
- **Short paragraphs** — 3 sentences max per paragraph
- **Tables over lists** when comparing or mapping (tasks to docs, config to purpose)
- **Code blocks** for commands, file paths, and configuration values
- **No marketing language** — skip superlatives, focus on what things do

## Output Instructions

1. Survey the entire codebase before writing anything — understand the directory
   structure, languages, frameworks, configuration, and existing documentation.
2. Create all documentation files as a single cohesive set. Every link in `docs/index.md`
   must point to a file you've created or verified exists.
3. Create a branch named `egg/onboarding-docs`, commit all doc files, and open a PR.
4. The PR should contain **only documentation files** — no code changes.
