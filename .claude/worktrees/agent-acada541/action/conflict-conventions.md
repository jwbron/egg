# Conflict Resolution Conventions

Guidelines for resolving merge conflicts via git merge.

## Merge Workflow

**Always use merge, not rebase.** Merging preserves the PR's commit history and
enables easy retry if resolution fails—the merge commit can be reverted without
losing any of the original work.

```bash
# 1. Fetch latest base branch
git fetch origin main

# 2. Preview the merge (see conflicts without committing)
git merge --no-commit origin/main

# 3. Analyze and categorize each conflict (see below)

# 4. For each conflict:
#    - Examine the conflict markers
#    - Resolve based on conflict type
#    - Stage resolved files: git add <files>

# 5. Commit the merge
git commit -m "Merge origin/main: resolve conflicts in <files>"

# 6. Push (no --force needed)
git push
```

**Never use `--force` or `--force-with-lease`** — merge commits use regular push.
If the push fails, something unexpected happened—investigate rather than forcing.

## Understanding Conflict Markers

When git encounters a conflict, it marks the file like this:

```
<<<<<<< HEAD
Content from the current branch (your PR)
=======
Content from the branch being merged in (main)
>>>>>>> origin/main
```

During a merge, "HEAD" is your current branch (the PR) and the bottom section is
what you're merging in (the base branch).

## Resolution Strategies

### Lock Files (package-lock.json, yarn.lock, poetry.lock, uv.lock)

**Always regenerate, never manually merge.**

```bash
# For npm
git checkout --theirs package-lock.json   # Accept main's version (being merged in)
npm install                                # Regenerate with PR's package.json

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

Note: During merge, `--ours` is the PR branch and `--theirs` is the base branch.
We take the base branch version then regenerate to include PR changes.

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

### Semantic Conflicts (Advanced)

Semantic conflicts occur when both sides modify the same logic differently. These
are the hardest to resolve correctly.

**Signs of a semantic conflict:**
- Same function body modified differently
- Same variable or constant changed to different values
- Same conditional logic altered in incompatible ways
- Same error handling modified differently

**Resolution strategies (when NOT escalating):**

1. **Check if changes are actually complementary:**
   - One side adds a feature, the other adds validation → include both
   - One side adds a parameter, the other adds a return type → include both

2. **Check if one side is a superset:**
   - If one change includes the other's intent plus more, take the larger change
   - Example: one side adds error handling, other adds same handling plus logging

3. **Check for temporal precedence:**
   - If commit messages indicate one change supersedes another, take the newer
   - Example: "Fix bug introduced in previous commit"

4. **When still uncertain, consider:**
   - What do the tests expect? Run tests to see which resolution they support
   - What does the PR description say the intent is?
   - Can you include both with minor adjustment (different variable names, etc.)?

**When to escalate semantic conflicts:**
- Both changes are valid but mutually exclusive (can't have both)
- The correct choice depends on product/business context
- You're not confident about the runtime behavior
- Tests don't clearly indicate which is correct

## When to Abort and Escalate

**Abort the merge and post a comment when:**

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
git merge --abort
```

Then post a comment explaining:
- Which files have conflicts
- What each side is trying to do
- Why human judgment is needed

## Recovery from Failed Resolution

If you pushed a merge commit that turned out to be incorrect (CI fails, logic error),
you can revert the merge and try again:

```bash
# Revert the merge commit (keep second parent's content)
git revert -m 1 HEAD

# Push the revert
git push

# Post a comment explaining what went wrong
# Then you can attempt a new merge with a different resolution
```

The `-m 1` option tells git to keep the first parent (the PR branch) and undo
the merge. This restores the branch to its pre-merge state.

## Common Failure Patterns

Learn from these common mistakes to avoid them:

### 1. Missing Import After Resolution
**Symptom:** `NameError`, `ImportError`, or `ModuleNotFoundError` after merge.
**Cause:** Kept one side's code that uses an import from the other side.
**Fix:** When resolving, always check that all imports needed by the final code are present.

### 2. Duplicate Function/Class Definitions
**Symptom:** `SyntaxError` or runtime shadowing issues.
**Cause:** Both sides added similar code and both were included without renaming.
**Fix:** If functions are truly duplicates, keep one. If slightly different, rename one.

### 3. Broken Lock File
**Symptom:** `npm install` or `pip install` fails.
**Cause:** Lock file was manually merged instead of regenerated.
**Fix:** Always regenerate lock files—never try to merge the JSON/YAML manually.

### 4. Incompatible Type Changes
**Symptom:** Type errors in TypeScript/mypy after merge.
**Cause:** One side changed a function signature, other side added code using old signature.
**Fix:** This is usually a semantic conflict—escalate rather than guessing.

### 5. Test Conflicts That Pass But Are Wrong
**Symptom:** Tests pass but behavior is incorrect.
**Cause:** Merged test expectations from both sides, hiding that one change broke the other.
**Fix:** Read test assertions carefully. If tests seem contradictory, escalate.

### 6. Partial Refactor Conflicts
**Symptom:** Mix of old and new patterns in the same file.
**Cause:** One side refactored a pattern, other side added code using old pattern.
**Fix:** Apply the refactor to all code, or escalate if refactor scope is unclear.

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
