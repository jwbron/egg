# Branch Recovery

In a pipeline session you are locked to a single work branch (your `assigned_branch`). The gateway denies branch switching, force-push, ref deletion, and most ref-mutation primitives. There is one exception, used for **detached-HEAD recovery**.

## When you might end up on detached HEAD

Some operations leave HEAD detached on success or failure — for example, a partway-through `git rebase --onto` or a `git switch --detach`. If you make a commit while detached, the commit is unreachable from your `assigned_branch` and the BRC `propose` step will not see it.

## How to recover

The gateway allows exactly one ref-mutation primitive, scoped to your own branch:

```
git update-ref refs/heads/<your-assigned-branch> <new-sha>
```

That sets your work branch ref to the new commit. The implementation does **not** require the new value to be a descendant of the current ref — you can also use this to rewind your branch after a bad direction, or repoint it to an arbitrary commit you've made or fetched. After running the command, `propose` (which pushes your branch to origin) will include the work.

**You will see a hint in stderr** after a `git commit` (successful or failed) whenever HEAD is detached, telling you the exact command to run. If you see it, run that command and continue.

## What is **not** allowed (and why)

- `git update-ref` to any ref other than `refs/heads/<your-assigned-branch>` — you can only update your own branch.
- `git update-ref --stdin` / `-d` / `-z` — only the safe two-arg form (`<ref> <newvalue> [<oldvalue>]`) is permitted; ref deletion and batch updates are not part of the supported recovery flow.
- `git branch -f` / `branch -d` — force/delete on the work branch is denied; use `update-ref` instead.
- `git checkout <branch>` / `git switch <branch>` — branch switching is denied in pipeline sessions.

## Don't keep guessing

If `update-ref` is rejected (wrong ref name, wrong session type, malformed args), the error message tells you exactly why. **Do not** loop on creative `git` invocations trying to bypass the policy. Fix the args and retry, or — if the orphan commit isn't worth saving — propose against the previous on-branch commit and abandon the detached work.

Background: issue #2162.
