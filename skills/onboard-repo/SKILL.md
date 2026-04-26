---
name: onboard-repo
description: Detect a repository's build / persist / checks shape and author a `.egg/repositories.yaml` file at the repo root, with AskUserQuestion confirmation for every detected entry and a `egg validate-config` pre-flight before write.
disable-model-invocation: true
argument-hint: "[<repo-path>] [--stdout]"
---

# Onboard Repo

You are guiding the user through onboarding a repository to egg. Your job is to
**detect** the repo's build / `persist` / `checks` shape, **confirm** every
detection with the human, **validate** the proposed YAML, and then **write** it
to `<repo>/.egg/repositories.yaml` (or print it to stdout when the repo is
read-only).

This skill replaces the hand-authoring path that previously lived in
`/egg-setup --update repos`. Per-repo `build_commands` / `persist` / `watch_files`
/ `checks` blocks now live in `<repo>/.egg/repositories.yaml` checked into the
target repo, not in `~/.config/egg/repositories.yaml`. Operator-scoped fields
(`writable_repos`, `github_username`, `default_reviewer`, `local_repos`,
`restrict_to_configured_users`, `disable_auto_fix`, etc.) still live in the
user file — see [`/egg-setup --update repos`](../egg-setup/SKILL.md) for that
flow.

## Argument Parsing

Parse the argument provided after `/onboard-repo`:

| Input | Interpretation |
|-------|---------------|
| `/onboard-repo` | Use `$EGG_REPO_PATH` (or `$PWD`) as the repo path |
| `/onboard-repo .` | Same — use the current working directory |
| `/onboard-repo /path/to/repo` | Onboard the repo at this absolute path |
| `/onboard-repo --stdout` | Skip the write; print the proposed YAML to stdout |
| `/onboard-repo /path/to/repo --stdout` | Combine both |

Validate that the resolved path exists and is a git repo (has a `.git/`
directory). If not, abort with a clear error.

## Phase 1 — Detection

Run the detector module and collect proposed `build_commands` / `persist` /
`checks` entries. Use Bash to invoke the egg CLI (the detector is registered as
a Python module so it can be unit-tested and re-used by other skills):

```bash
egg validate-config --detect <repo-path>
```

(or call `python -c "from shared.egg_config.onboard_detectors import run_detectors; ..."` directly).

The detector returns a list of `DetectionResult` records — one per language —
each carrying:

- `language` — `"python-uv"`, `"python-pip"`, `"node-npm"`, `"node-pnpm"`,
  `"node-yarn"`, or `"go"` (v1 — see [`docs/guides/repo-config.md`](../../docs/guides/repo-config.md)
  for the up-to-date list).
- `build_commands` — proposed `watch_files` + `commands` block.
- `persist` — proposed unified persist list (entries beginning with `/` are
  classified as system-absolute on the host; others are repo-relative).
- `checks` — proposed `[{name, command}, ...]` from `Makefile` targets and/or
  `package.json` `scripts`.
- `confidence` — `0.0`–`1.0` heuristic confidence.
- `reasoning` — free-text explanation citing the specific lockfile / manifest
  the detector matched on.

**Mixed-language repos** return multiple results; the skill merges them
(concat `watch_files`, concat `persist`, concat `checks`, concat `commands` in
language order).

If `run_detectors` returns an empty list, present the user with a short
explanation of why no language was detected and ask whether they want to
hand-author the block (point them at
[`docs/guides/repo-config.md`](../../docs/guides/repo-config.md)).

## Phase 2 — Confirmation

For **every** detected entry, call `AskUserQuestion` to confirm. Per Q6 of
[issue #2073](https://github.com/jwbron/egg/issues/2073), there is **no
confidence-based shortcut** — even high-confidence detections require human
sign-off.

### Step 1: Confirm the language(s)

Use `AskUserQuestion` (multiSelect when more than one language fired):

- **Question**: "I detected the following project types in this repo. Which should I include in the `.egg/repositories.yaml` block?"
- **Header**: "Languages"
- **multiSelect**: true (only when >1 language)
- **Options**: one entry per detected language with the detector's
  `reasoning` text as the option description (e.g., `"Python (uv) — found pyproject.toml + uv.lock"`).

### Step 2: Confirm `build_commands`

For each selected language, surface the detector's proposed
`build_commands` block and ask:

- **Question**: "Use the detected build_commands for `<language>`?"
- **Header**: "Build cmds"
- **Options**:
  - **"Use as detected (Recommended)"** — description: shows the proposed
    `commands:` list verbatim
  - **"Edit before writing"** — description: "Open the proposed YAML for
    manual edits, then re-validate"
  - **"Skip this language"** — description: "Drop this language's
    build_commands; keep its persist/checks if confirmed below"

If "Edit before writing", present the YAML in a fenced block, take the
user's revisions, and re-run validation in Phase 3 against the edited block.

### Step 3: Confirm `persist`

For each detected `persist:` entry, ask:

- **Question**: "Persist `<entry>` (classified as `<repo|system>`)?"
- **Header**: "Persist"
- **Options**:
  - **"Yes (Recommended)"** — description: detector's reasoning (e.g.,
    `".venv is the uv project virtualenv"`)
  - **"No"** — description: "Drop this entry; the build will recreate it
    from source if needed"

Per [decision-3](../../.egg-state/contracts/issue-2073.json), entries
beginning with `/` are absolute system paths; others are repo-relative. The
host-side classifier produces the legacy two-list manifest shape from the
unified `persist:` list, so this classification only affects how the entry is
routed inside the manifest — the user-facing schema stays a single list.

### Step 4: Confirm `checks`

For each detected `checks` entry (one per `Makefile` target or `package.json`
script that the detector matched), ask:

- **Question**: "Add check `<name>` running `<command>`?"
- **Header**: "Checks"
- **Options**:
  - **"Yes (Recommended)"** — description: shows the resolved command
  - **"No"** — description: "Skip this check; the SDLC pipeline will use
    the global default (`make lint && make test`)"
  - **"Rename"** — description: "Use the same command but a different display
    name (useful when you have multiple lint targets)"

### Step 5: Optional fields

Use `AskUserQuestion` (multiSelect):

- **Question**: "Configure any optional per-repo fields?"
- **Header**: "Options"
- **multiSelect**: true
- **Options**:
  - **"auth_mode: user"** — description: "Use the operator's PAT
    (`GITHUB_USER_TOKEN`) for this repo instead of the bot identity. Requires
    `GITHUB_USER_TOKEN` set in `secrets.env`."
  - **"checkpoint_repo"** — description: "Push checkpoints to a separate repo
    for transcript privacy. Format: `owner/repo`."
  - **"template (reserved)"** — description: "Reserved for a future template
    library (issue #2073 Q4). Accepts `string | null`; leave unset for now
    unless you know a template is published."
  - **"None"** — description: "Use defaults"

For each selected option, collect the value and add it to the rendered block.

## Phase 3 — Validation Pre-flight

**Before writing anything**, render the proposed block to a temporary YAML
file and run the validator:

```bash
egg validate-config --repo-config /tmp/onboard-repo-<rand>.yaml
```

Per [decision-11](../../.egg-state/contracts/issue-2073.json), the validator
returns errors and warnings:

- **Errors** block the write. Show each error to the user with the offending
  field and the recommended fix; loop back to Phase 2 to revise.
- **Warnings** advise but do not block. Surface every warning to the user with
  `AskUserQuestion`:
  - **Question**: "The validator reported `<N>` warning(s). Continue and
    write the file?"
  - **Header**: "Warnings"
  - **Options**:
    - **"Write anyway (Recommended)"** — description: "Acknowledge the
      warnings and proceed"
    - **"Revise"** — description: "Loop back to Phase 2 to edit the block"
    - **"Abort"** — description: "Print the YAML to stdout and exit"

The validator's heuristic checks include (non-exhaustive — see
[`docs/guides/repo-config.md`](../../docs/guides/repo-config.md) §"Validator
checks"):

- `build_commands` install something to a system path but no `persist:` entry
  covers it (error).
- `uv sync` / `pip install -e .` / `npm install` paired with a watch-files-only
  build context (warning, with the `--no-install-project` suggestion — the
  [#2087](https://github.com/jwbron/egg/issues/2087) trap).
- `checks.command` references a Makefile target that doesn't exist (error).
- `auth_mode: user` without `GITHUB_USER_TOKEN` configured at validate-time
  (warning — runtime injection paths exist).
- `persist:` entry pointing to an empty directory (warning).
- `build_commands` exec `curl` / `wget` while egg's network-locked private
  mode is in effect (warning, network-mode-only condition).

## Phase 4 — Write or Stdout-fallback

### Default: write to `<repo>/.egg/repositories.yaml`

If the repo is writable, render the block to `<repo>/.egg/repositories.yaml`.
If the file already exists, use `AskUserQuestion`:

- **Question**: "`<repo>/.egg/repositories.yaml` already exists. What should I
  do?"
- **Header**: "Conflict"
- **Options**:
  - **"Back up and overwrite"** — description: "Save existing as
    `repositories.yaml.bak-<timestamp>` and write the new block"
  - **"Merge in place"** — description: "Take the existing block as a base
    and apply each Phase-2-confirmed entry on top (replace-by-default for
    list-valued fields)"
  - **"Print to stdout instead"** — description: "Skip the write; show the
    proposed YAML so the user can hand-edit"

### Fallback: stdout + offer user-file append

Per [decision-8](../../.egg-state/contracts/issue-2073.json), when the repo
isn't writable (e.g., the agent has no permission, the user passed
`--stdout`, or the user picked "Print to stdout instead" above):

1. Print the proposed YAML to stdout in a fenced block.
2. Use `AskUserQuestion`:
   - **Question**: "I can't write to the repo. Should I append this block to
     your user file (`~/.config/egg/repositories.yaml`) under
     `repo_settings.<owner/repo>` instead?"
   - **Header**: "Fallback"
   - **Options**:
     - **"Append to user file (Recommended)"** — description: "Adds the block
       to `repo_settings:` in the user file. The repo file will still take
       precedence if added later."
     - **"Just print to stdout"** — description: "I'll copy-paste it myself"
     - **"Abort"** — description: "Don't do anything"

If "Append to user file", read the existing user file, deep-merge the block
under `repo_settings:`, and write back atomically. **Replace-by-default** for
list-valued fields per
[decision-9](../../.egg-state/contracts/issue-2073.json) — the user file
overwrites repo-defaults for `persist`, `watch_files`, `checks`,
`extra_packages.apt|dnf`, and `local_repos.paths`.

## Phase 5 — Post-write summary

After a successful write, present:

```
## Onboarding Complete

| File | Status |
|------|--------|
| <repo>/.egg/repositories.yaml | Written |
| Validator | 0 errors, <N> warnings |

### Block written

```yaml
schemaVersion: "1.0"
build_commands:
  watch_files:
    - pyproject.toml
    - uv.lock
  commands:
    - uv sync --frozen --no-install-project
persist:
  - .venv
  - /usr/local/bin/uv
checks:
  - {name: lint, command: make lint}
  - {name: test, command: make test}
```

### Next steps

- Commit `<repo>/.egg/repositories.yaml` to the repo so the rest of the
  team / fleet picks it up automatically.
- If you have a per-repo block in `~/.config/egg/repositories.yaml` for this
  repo, you can now delete it (the loader auto-discovers the repo file).
- Run `egg validate-config <repo>` periodically — the validator surfaces new
  drift as the build / persist / checks shape evolves.
- For per-operator overrides, edit
  `repo_settings.<owner/repo>` in `~/.config/egg/repositories.yaml`. The
  user file takes precedence at the leaf level; list-valued fields are
  replaced outright.
```

## `template:` reserve field

The schema reserves a `template:` field (string | null) on every per-repo
block (issue [#2073](https://github.com/jwbron/egg/issues/2073) Q4) so a
follow-up template library can land without another schema migration. The
v1 loader accepts `template: null` and `template: "<some-name>"` but does
**not** consume it — the field is forward-compatible only. The skill
documents this in Phase 2 Step 5 so onboarders see it without having to
read the schema source.

## Detector extensibility (Q7)

The detector lives at `shared/egg_config/onboard_detectors.py` as a
pluggable module. Repos that deviate from the simple
`lint`/`test` Makefile convention (e.g., `bazel test //...`, gradle
wrappers, multi-stage checks) can register a custom `Detector` via
`register_detector(detector)` without modifying the built-in heuristics.
The detector is unit-tested (see
`tests/shared/egg_config/test_onboard_detectors.py`); plug-ins should
follow the same pattern.

## Critical Rules

- **Always confirm every detection** — no auto-write on high confidence
  (Q6).
- **Always run `egg validate-config` before write** — surface errors and
  warnings to the user; abort on errors.
- **Never write outside the target repo** — the only writable destinations
  are `<repo>/.egg/repositories.yaml` (primary) and
  `~/.config/egg/repositories.yaml` (fallback under `repo_settings`).
- **Replace-by-default** for list-valued fields when merging onto an
  existing block — no `extends:` keyword in v1 (decision-9).
- **Reject operator-scoped keys** in the repo file — `writable_repos`,
  `github_username`, `bot_username`, `default_reviewer`, `local_repos`,
  `restrict_to_configured_users`, `disable_auto_fix` belong in the user
  file. The loader rejects them in the repo file with a clear error
  (decision-15 / NACK-5).
- **Honor the persist denylist** in the repo file — `/etc`, `/root`,
  `/var`, `/home/`, `/proc`, `/sys`, `/dev`, `/.ssh`, and any path
  outside `/usr/local/`, `/opt/`, or the repo root are blocked by the
  loader. The validator catches them at write-time. Operator-side
  `persist:` (in the user file) accepts any path.
- **Keep output concise** — use checklists and tables, not verbose
  paragraphs.
