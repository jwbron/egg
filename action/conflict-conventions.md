# Conflict Resolution Conventions

Guidelines for resolving merge conflicts via git rebase.

## Rebase Workflow

**Always use rebase, not merge.** Rebasing maintains a linear history and makes
conflicts explicit at each commit.

```bash
# 1. Fetch latest base branch
git fetch origin main

# 2. Start rebase
git rebase origin/main

# 3. For each conflict:
#    - Resolve the files
#    - Stage resolved files: git add <files>
#    - Continue: git rebase --continue

# 4. After rebase completes, push with lease
git push --force-with-lease
```

**Never use `--force`** — always use `--force-with-lease` which fails if someone
else pushed to the branch, preventing accidental overwrites.

## Understanding Conflict Markers

When git encounters a conflict, it marks the file like this:

```
<<<<<<< HEAD
Content from the base branch (main)
=======
Content from the PR branch
>>>>>>> commit-message
```

During a rebase, "HEAD" is the base branch and the bottom section is your change.

## Resolution Strategies

### Lock Files (package-lock.json, yarn.lock, poetry.lock, uv.lock)

**Always regenerate, never manually merge.**

```bash
# For npm
git checkout --theirs package-lock.json  # Accept main's version
npm install                               # Regenerate with PR's package.json

# For yarn
git checkout --theirs yarn.lock
yarn install

# For poetry
git checkout --theirs poetry.lock
poetry lock

# For uv
git checkout --theirs uv.lock
uv lock
```

### Additive Changes (Both Sides Add Content)

When both sides add different things to the same location (e.g., new entries in
a list, new functions in a file), include both:

```python
# Before (conflict):
<<<<<<< HEAD
def new_function_from_main():
    pass
=======
def new_function_from_pr():
    pass
>>>>>>> feature-branch

# After (resolved):
def new_function_from_main():
    pass

def new_function_from_pr():
    pass
```

### Import Conflicts

When both sides add different imports, include all imports and sort them:

```python
# Resolved: include both, maintain sort order
from module_a import ClassA
from module_b import ClassB  # from main
from module_c import ClassC  # from PR
```

### Version Bumps

When both sides bump versions differently:
- For patch versions: take the higher version
- For minor/major versions: abort and escalate (may indicate conflicting features)

### Configuration Files

Configuration conflicts often need human review because they may represent:
- Different feature flags for different environments
- Conflicting architectural decisions
- Security-sensitive settings

**Escalate configuration conflicts unless they're clearly additive.**

## When to Abort and Escalate

**Abort the rebase and post a comment when:**

1. **Semantic conflicts** — Both sides modify the same logic differently, and you
   can't determine which behavior is correct without product context.

2. **API breaking changes** — One side modifies an API signature that the other
   side depends on.

3. **Security-sensitive code** — Conflicts in authentication, authorization,
   encryption, or access control code. These need human review.

4. **Database migrations** — Migration conflicts can cause data issues. Always
   escalate.

5. **Test conflicts** — If tests conflict in ways that suggest the implementations
   are incompatible, escalate rather than guessing which tests should pass.

6. **Large conflicts** — More than 5 files with non-trivial conflicts, or any
   single file with conflicts spanning more than 50 lines.

To abort:

```bash
git rebase --abort
```

Then post a comment explaining:
- Which files have conflicts
- What each side is trying to do
- Why human judgment is needed

## Post-Resolution Verification

**Always verify after resolving:**

```bash
# Run the project's checks
make lint    # or: ruff check ., npm run lint
make test    # or: pytest, npm test
make build   # or: npm run build

# If any fail, fix before pushing
```

Only push after all checks pass locally.

## Decision Framework

| Conflict Type | Action | Reason |
|--------------|--------|--------|
| Lock files | Regenerate | Merging lock files creates invalid state |
| Additive changes | Include both | Both additions are likely intentional |
| Import order | Include all, sort | Formatting, not semantic |
| Whitespace/formatting | Accept either | Use linter to normalize |
| Semantic logic | Escalate | Requires understanding intent |
| API signatures | Escalate | May break dependents |
| Security code | Escalate | Needs human review |
| Config files | Usually escalate | May be environment-specific |
| Migrations | Escalate | Risk of data issues |

## Signature

End all PR comments with: — Authored by egg
