# The plan in plain terms — issue #3393

**Six steps, done in a fixed order, that teach the pipeline system to work
across several GitHub repositories at once — opening one pull request per
unit of work, each in the right repo, in the right order.**

One orientation note: all six steps change the pipeline system itself (the
egg repo). This project *builds* the multi-repo capability; it doesn't span
repos itself.

## Why a fixed order instead of parallel work

Five of the six steps edit the same very large source file, and steps 1 and 3
touch another shared file. Work that touches the same file in parallel
collides when the pieces are merged back together, so the steps form one
straight line — each builds on the previous. The order is also
value-ordered: the data-format foundation lands first, because everything
else reads it.

## The six steps

1. **Teach the saved plan format about repos.** Every unit of work ("slice")
   gets a `repo` field — exactly one repo each — and a pipeline now carries a
   *list* of repos, each with its own starting branch. The saved format's
   version number is bumped, and old saved plans upgrade automatically when
   loaded (a slice without a repo is assigned the pipeline's primary repo).
   This kind of upgrade has been done four times before in this codebase.
   Single-repo pipelines are unaffected.

2. **Accept a list of repos at submission, with the two safety checks.**
   Submitting a pipeline now takes any number of repos (each with its own
   starting branch); submitting a single repo the old way still works. The
   submission is rejected — with an error naming the offending repos — if
   the repos mix private and public, or mix sign-in methods. Two repos that
   happen to share a name are fine (step 3 fixes the underlying ambiguity),
   per the operator's ruling. The first repo in the list is the "primary"
   one (used for naming and as the default), unless the submitter explicitly
   marks another.

3. **Stop ignoring every repo after the first.** Three places in the code
   quietly kept only the first repo of the list; all three now use the
   proper "primary" concept or the full list. Agents now receive the full
   map of repo → checked-out folder, keyed by the repo's full
   owner-plus-name (the operator-ruled fix for the name-collision trap). A
   watchdog test fails the build if anyone reintroduces the old
   first-repo-only shortcut.

4. **Open each PR in its own repo.** A slice's PR is created in that slice's
   repo. Every repo that has at least one slice also gets its own working
   branch and umbrella PR — same conventions as today's single repo; repos
   submitted but left without work get neither. All the PRs in a pipeline
   reference each other so reviewers can see the whole picture.

5. **The ordering hold, exactly as the operator decided.** When one slice
   depends on a slice in a *different* repo, its PR opens as a **draft**, and
   the system watches the upstream PR: the moment it merges, the draft is
   automatically flipped to ready — no human needed for plain merge
   ordering. Anything beyond merge order — waiting for a release of the
   upstream repo, choosing a version to pin, or work that genuinely cannot
   proceed — is a separate kind of hold that only a human decision releases.
   Importantly, the dependency never delays the *work* itself, only when the
   PR can merge.

6. **Test and review each slice inside its own repo.** The test gate runs in
   the slice's repo checkout only; the review diff compares against that
   repo's own base branch; and the agent works under that repo's own house
   rules (instructions file, linters, check commands) — egg's own commands
   apply only to egg slices. The documentation is updated to describe the
   new model.

## How it's verified

Each step ships with its own tests (format upgrades, submission checks, the
watchdog, PR routing, both hold types, per-repo scoping), and the full test
suite plus linting must stay green throughout. The standing regression
guarantee: **a single-repo pipeline behaves exactly as it does today.**

## After it lands

Nothing manual is needed. Old saved plans upgrade themselves the first time
they're loaded, and the new behavior applies to pipelines submitted after the
normal release rollout. The consciously deferred items — mixing sign-in
methods across repos, and fancier ordering machinery than the v1 hold — get
filed as a follow-up issue.
