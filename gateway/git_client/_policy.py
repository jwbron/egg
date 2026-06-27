"""Static git-validation policy tables (allowlists / flag maps).

Pure data extracted verbatim from the pre-split ``gateway/git_client.py``
(#3312 slice-11). The large ``GIT_ALLOWED_COMMANDS`` per-operation
allowlist dominates this module; keeping the data separate from the
validation logic (``_validation``) keeps both under the size cap.
"""


# =============================================================================
# Path Validation
# =============================================================================

# Allowed base paths for repo_path validation
# These are the only directories where git operations are permitted
ALLOWED_REPO_PATHS = [
    "/home/egg/repos/",
    "/home/egg/.egg-worktrees/",
    "/home/egg/.egg-state/",  # Pipeline state worktree
    "/repos/",  # Legacy path
]


# Directories that contain repos but are NOT repos themselves
# Git operations in these directories are expected to fail
REPOS_PARENT_DIRECTORIES = [
    "/home/egg/repos",
    "/home/egg/.egg-worktrees",
    "/repos",
]


# =============================================================================
# Argument Validation
# =============================================================================

# Explicitly dangerous git flags - never allowed regardless of operation
# These could be used for command injection or config override attacks
BLOCKED_GIT_FLAGS = [
    "--upload-pack",  # Can specify arbitrary command
    "--exec",  # Can specify arbitrary command
    "--config",  # Config override (could disable security)
    "--receive-pack",  # Arbitrary command execution
]


# Allowed values for flags that take restricted arguments
# Maps flag name to set of allowed values
ALLOWED_FLAG_VALUES: dict[str, set[str]] = {
    "--strategy-option": {
        "ours",
        "theirs",
        "patience",
        "ignore-space-change",
        "ignore-all-space",
        "ignore-space-at-eol",
    },
}


# Per-operation allowlist of flags that are permitted
# This is more secure than a blocklist - unknown flags are rejected by default
GIT_ALLOWED_COMMANDS: dict[str, dict[str, list[str]]] = {
    # === Network operations (require authentication) ===
    "fetch": {
        "allowed_flags": [
            "--all",
            "--tags",
            "--prune",
            "--depth",
            "--shallow-since",
            "--shallow-exclude",
            "--jobs",
            "--no-tags",
            "--force",
            "--verbose",
            "--quiet",
            "--dry-run",
            "--recurse-submodules",
            "--progress",
            "--no-progress",
            "--unshallow",
            "--deepen",
        ],
    },
    "ls-remote": {
        "allowed_flags": [
            "--heads",
            "--tags",
            "--refs",
            "--quiet",
            "--exit-code",
            "--get-url",
            "--sort",
            "--symref",
        ],
    },
    "push": {
        "allowed_flags": [
            "--force",
            "--force-with-lease",
            "--tags",
            "--delete",
            "--set-upstream",
            "--verbose",
            "--quiet",
            "--dry-run",
            "--no-verify",
        ],
    },
    # === Local read operations ===
    "status": {
        "allowed_flags": [
            "--porcelain",
            "--short",
            "--branch",
            "--show-stash",
            "--long",
            "--verbose",
            "--untracked-files",
            "--ignored",
            "--no-ahead-behind",
            "-sb",
            "-s",
        ],
    },
    # ``--patch`` (``-p``) and ``--not`` power the canonical BRC re-review
    # delta command documented in REVIEWER-SYNC.md:110 and emitted by
    # ``_build_review_prompt()`` in ``orchestrator/routes/pipelines.py``
    # (``git log <sha>..HEAD --not origin/<base> -p``). Both are read-only.
    # ``--not`` mirrors the ``^ref`` exclude syntax the revision-range parser
    # already accepts (see ``agent_salvage.py`` ``_resolve_anchor`` /
    # ``list_unpushed_commits``). Without these, reviewers on the LiteLLM
    # route burn their turn budget retrying and exit without a v2+ verdict
    # (#2905).
    #
    # ``-n N``, ``-n=N``, ``-nN``, and ``-<N>`` (e.g. ``-3``) are accepted as
    # aliases for ``--max-count=N`` via special cases in ``validate_git_args``
    # (search for "issue #2480"). We don't put ``-n`` in the ``log`` entry of
    # ``FLAG_NORMALIZATION`` because its value is a separate argument, which
    # the per-flag normalizer can't see. Both special cases also apply to the
    # ``reflog`` allowlist below — reflog is internally a log walker and
    # shares the same ``--max-count`` semantics for both forms.
    "log": {
        "allowed_flags": [
            "--oneline",
            "--graph",
            "--all",
            "--decorate",
            "--stat",
            "--name-only",
            "--name-status",
            "--format",
            "--pretty",
            "--abbrev-commit",
            "--no-merges",
            "--merges",
            "--first-parent",
            "--reverse",
            "--max-count",
            "--patch",
            "--not",
            "--since",
            "--until",
            "--author",
            "--grep",
            "--follow",
            "--diff-filter",
        ],
    },
    "diff": {
        "allowed_flags": [
            "--cached",
            "--staged",
            "--stat",
            "--numstat",
            "--shortstat",
            "--name-only",
            "--name-status",
            "--diff-filter",
            "--color",
            "--no-color",
            "--word-diff",
            "--ignore-space-change",
            "--ignore-all-space",
            "--ignore-blank-lines",
            "--no-index",
            "--unified",
            "-U",
        ],
    },
    "diff-tree": {
        "allowed_flags": [
            "--no-commit-id",
            "--name-only",
            "--name-status",
            "--stat",
            "--numstat",
            "--diff-filter",
            "--format",
            "--pretty",
            "--abbrev-commit",
            "-r",
            "-t",
            "-p",
        ],
    },
    "show": {
        "allowed_flags": [
            "--stat",
            "--name-only",
            "--name-status",
            "--format",
            "--pretty",
            "--abbrev-commit",
            "--no-patch",
            "-s",
        ],
    },
    "branch": {
        "allowed_flags": [
            "--list",
            "--all",
            "--remotes",
            "--verbose",
            "--merged",
            "--no-merged",
            "--contains",
            "--sort",
            "--format",
            "--show-current",
            "-a",
            "-r",
            "-v",
            "-vv",
        ],
    },
    "rev-parse": {
        "allowed_flags": [
            "--abbrev-ref",
            "--short",
            "--verify",
            "--symbolic-full-name",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
            "--is-inside-work-tree",
            "--is-bare-repository",
        ],
    },
    "ls-tree": {
        "allowed_flags": [
            "--name-only",
            "--name-status",
            "--full-name",
            "--full-tree",
            "--long",
            "-r",
            "-t",
            "-d",
            "-l",
        ],
    },
    "remote": {
        "allowed_flags": [
            "--verbose",
            "-v",
        ],
    },
    "worktree": {
        "allowed_flags": [
            "--porcelain",
            "--verbose",
            "-v",
        ],
    },
    "ls-files": {
        "allowed_flags": [
            "--cached",
            "--deleted",
            "--modified",
            "--others",
            "--ignored",
            "--stage",
            "--unmerged",
            "--killed",
            "--full-name",
            "--error-unmatch",
            "--exclude-standard",
            "-c",
            "-d",
            "-m",
            "-o",
            "-i",
            "-s",
            "-u",
            "-k",
        ],
    },
    # === Local write operations ===
    "add": {
        "allowed_flags": [
            "--all",
            "--update",
            "--force",
            "--dry-run",
            "--verbose",
            "--patch",
            "--intent-to-add",
            "-A",
            "-u",
            "-f",
            "-n",
            "-v",
            "-p",
            "-N",
        ],
    },
    "update-index": {
        "allowed_flags": [
            "--chmod",
            "--refresh",
            "--really-refresh",
            "--verbose",
            "--quiet",
            "-q",
        ],
    },
    "commit": {
        "allowed_flags": [
            "--message",
            "--all",
            "--amend",
            "--no-edit",
            "--allow-empty",
            "--allow-empty-message",
            "--author",
            "--date",
            "--dry-run",
            "--verbose",
            "--quiet",
            "--signoff",
            "-m",
            "-a",
            "-v",
            "-q",
            "-s",
        ],
    },
    "checkout": {
        "allowed_flags": [
            "--force",
            "--ours",
            "--theirs",
            "--merge",
            "--quiet",
            "--track",
            "--no-track",
            "-b",
            "-B",
            "-f",
            "-q",
            "-t",
        ],
    },
    "switch": {
        "allowed_flags": [
            "--create",
            "--force-create",
            "--detach",
            "--quiet",
            "--track",
            "--no-track",
            "-c",
            "-C",
            "-d",
            "-q",
            "-t",
        ],
    },
    "reset": {
        "allowed_flags": [
            "--soft",
            "--mixed",
            "--hard",
            "--merge",
            "--keep",
            "--quiet",
            "-q",
        ],
    },
    "restore": {
        "allowed_flags": [
            "--staged",
            "--worktree",
            "--source",
            "--quiet",
            "-S",
            "-W",
            "-s",
            "-q",
        ],
    },
    "stash": {
        "allowed_flags": [
            "--keep-index",
            "--include-untracked",
            "--all",
            "--quiet",
            "--message",
            "-k",
            "-u",
            "-a",
            "-q",
            "-m",
        ],
    },
    "merge": {
        "allowed_flags": [
            "--no-commit",
            "--no-ff",
            "--ff-only",
            "--squash",
            "--abort",
            "--continue",
            "--quit",
            "--message",
            "--no-edit",
            "--strategy-option",
            "--verbose",
            "--quiet",
            "-X",
            "-m",
            "-v",
            "-q",
        ],
    },
    "rebase": {
        "allowed_flags": [
            "--onto",
            "--autostash",
            "--abort",
            "--continue",
            "--skip",
            "--quit",
            "--interactive",
            "--verbose",
            "--quiet",
            "-i",
            "-v",
            "-q",
        ],
    },
    "cherry-pick": {
        "allowed_flags": [
            "--abort",
            "--continue",
            "--skip",
            "--quit",
            "--no-commit",
            "--edit",
            "--mainline",
            "-n",
            "-e",
            "-m",
        ],
    },
    "am": {
        "allowed_flags": [
            "--abort",
            "--continue",
            "--skip",
            "--quit",
            "--resolved",
            "--no-verify",
            "--keep",
            "--keep-non-patch",
            "--message-id",
            "--scissors",
            "--no-scissors",
            "--ignore-whitespace",
            "--whitespace",
            "--directory",
            "--3way",
            "--quiet",
            "-k",
            "-m",
            "-s",
            "-3",
            "-q",
        ],
    },
    "apply": {
        "allowed_flags": [
            "--stat",
            "--numstat",
            "--summary",
            "--check",
            "--index",
            "--cached",
            "--3way",
            "--reverse",
            "--reject",
            "--verbose",
            "--quiet",
            "--whitespace",
            "--directory",
            "--unidiff-zero",
            "-p",
            "-v",
            "-R",
            "-3",
            "-q",
        ],
    },
    "format-patch": {
        "allowed_flags": [
            "--stdout",
            "--output-directory",
            "--numbered",
            "--start-number",
            "--no-numbered",
            "--keep-subject",
            "--signoff",
            "--cover-letter",
            "--quiet",
            "--no-stat",
            "--stat",
            "--notes",
            "--base",
            "-o",
            # Note: -n is not in the format-patch allowlist; users should use --numbered instead.
            "-N",
            "-k",
            "-s",
            "-q",
        ],
    },
    "tag": {
        "allowed_flags": [
            "--list",
            "--delete",
            "--annotate",
            "--message",
            "--force",
            "--sign",
            "--verify",
            "-l",
            "-d",
            "-a",
            "-m",
            "-f",
            "-s",
            "-v",
        ],
    },
    "clean": {
        "allowed_flags": [
            "--force",
            "--dry-run",
            "--quiet",
            "-d",
            "-f",
            "-n",
            "-q",
            "-x",
            "-X",
        ],
    },
    "rm": {
        "allowed_flags": [
            "--force",
            "--dry-run",
            "--cached",
            "--quiet",
            "-f",
            "-n",
            "-r",
            "-q",
        ],
    },
    "mv": {
        "allowed_flags": [
            "--force",
            "--dry-run",
            "--verbose",
            "-f",
            "-n",
            "-v",
            "-k",
        ],
    },
    "blame": {
        "allowed_flags": [
            "--line-porcelain",
            "--porcelain",
            "--incremental",
            "--show-stats",
            "--show-name",
            "--show-number",
            "--show-email",
            "-L",
            "-l",
            "-t",
            "-w",
            "-e",
            "-n",
            "-s",
            "-f",
        ],
    },
    "reflog": {
        "allowed_flags": [
            "--all",
            "--date",
            "--format",
            "--oneline",
            "--max-count",
            # ``-n N``, ``-n=N``, ``-nN``, and ``-<N>`` (e.g. ``-3``) are
            # accepted as aliases for ``--max-count=N`` via the same special
            # cases in ``validate_git_args`` that handle ``log`` (search for
            # "issue #2480"). reflog is internally a log walker, so both
            # forms carry the same ``--max-count`` semantics.
        ],
    },
    "describe": {
        "allowed_flags": [
            "--tags",
            "--all",
            "--long",
            "--abbrev",
            "--always",
            "--dirty",
            "--broken",
            "--match",
            "--exclude",
            "--first-parent",
            "--contains",
        ],
    },
    "config": {
        "allowed_flags": [
            "--get",
            "--get-all",
            "--list",
            "--local",
            "--global",
            "-l",
        ],
    },
    "merge-base": {
        "allowed_flags": [
            "--all",
            "--octopus",
            "--independent",
            "--is-ancestor",
            "--fork-point",
        ],
    },
    "cat-file": {
        "allowed_flags": [
            "-p",
            "-t",
            "-s",
            "-e",
            "--batch",
            "--batch-check",
        ],
    },
    # update-ref is restricted to the safe two-arg form
    # (`update-ref <ref> <newvalue> [<oldvalue>]`). The gateway additionally
    # scopes the target ref to ``refs/heads/<assigned_branch>`` for pipeline
    # sessions and force-prepends ``--no-deref`` server-side (defense in depth
    # against symref-following) — see the update-ref guard in gateway.py.
    # ``--stdin``/``-d``/``-z``/``--create-reflog`` are intentionally absent
    # from the allowlist (and therefore rejected) to keep the surface area
    # tight: ref deletion and batch updates are not part of the supported
    # recovery flow. Issue #2162.
    "update-ref": {
        "allowed_flags": [],
    },
    # symbolic-ref is the canonical reattach primitive when an agent ends up
    # on detached HEAD (e.g. after a `git rebase` against an advanced
    # upstream silently leaves the worktree detached).  Restricted to the
    # two-arg form (`symbolic-ref HEAD <ref>`) and further scoped in
    # gateway.py to the session's assigned branch or the per-role local
    # work branch.  Lower-level than `switch`/`checkout`: rewrites HEAD's
    # symref but does not change branch contents.  Pairs with the
    # already-allowed `git reset --hard <on-lineage>` to advance the
    # local branch when needed.  ``-d``/``--delete``/``--short``/``-q``
    # are intentionally absent: deletion is not a recovery primitive and
    # the printed-output forms have no use in scripted recovery.
    # Issue #2200.
    "symbolic-ref": {
        "allowed_flags": [],
    },
}


# Per-subcommand flag normalization: map short flags to long form for consistent
# validation.  Short flags that don't have an obvious long form (like ``blame -L``,
# ``ls-tree -r``, ``clean -d``) are intentionally NOT normalized — they remain
# as-is and pass through the allowlist check unchanged (allowlists already contain
# these short forms).
FLAG_NORMALIZATION = {
    "fetch": {
        "-a": "--all",
        "-t": "--tags",
        "-p": "--prune",
        "-v": "--verbose",
        "-q": "--quiet",
        "-j": "--jobs",
    },
    "push": {
        "-f": "--force",
        "-d": "--delete",
        "-u": "--set-upstream",
        "-n": "--dry-run",
        "-v": "--verbose",
        "-q": "--quiet",
    },
    "add": {
        "-A": "--all",
        "-u": "--update",
        "-f": "--force",
        "-n": "--intent-to-add",
        "-N": "--intent-to-add",
        "-v": "--verbose",
        "-p": "--patch",
    },
    "stash": {
        "-k": "--keep-index",
        "-u": "--include-untracked",
        "-a": "--all",
        "-q": "--quiet",
        "-m": "--message",
    },
    "checkout": {"-f": "--force", "-q": "--quiet", "-t": "--track"},
    "switch": {
        "-c": "--create",
        "-C": "--force-create",
        "-d": "--detach",
        "-q": "--quiet",
        "-t": "--track",
    },
    "commit": {
        "-m": "--message",
        "-a": "--all",
        "-v": "--verbose",
        "-q": "--quiet",
        "-s": "--signoff",
    },
    "tag": {
        "-l": "--list",
        "-d": "--delete",
        "-a": "--annotate",
        "-m": "--message",
        "-f": "--force",
        "-s": "--sign",
        "-v": "--verify",
    },
    "blame": {"-e": "--show-email", "-n": "--show-number", "-f": "--show-name"},
    "cherry-pick": {"-n": "--no-commit", "-e": "--edit", "-m": "--mainline"},
    "apply": {"-v": "--verbose", "-R": "--reverse", "-q": "--quiet"},
    "ls-files": {
        "-c": "--cached",
        "-d": "--deleted",
        "-m": "--modified",
        "-o": "--others",
        "-i": "--ignored",
        "-s": "--stage",
        "-u": "--unmerged",
        "-k": "--killed",
    },
    "clean": {"-f": "--force", "-n": "--dry-run", "-q": "--quiet"},
    "rm": {"-f": "--force", "-n": "--dry-run", "-q": "--quiet"},
    "mv": {"-f": "--force", "-n": "--dry-run", "-v": "--verbose"},
    "merge": {"-m": "--message", "-v": "--verbose", "-q": "--quiet", "-X": "--strategy-option"},
    "rebase": {"-v": "--verbose", "-q": "--quiet"},
    "reset": {"-q": "--quiet"},
    "restore": {"-S": "--staged", "-W": "--worktree", "-s": "--source", "-q": "--quiet"},
    "am": {"-k": "--keep", "-m": "--message-id", "-s": "--scissors", "-q": "--quiet"},
    "format-patch": {
        "-o": "--output-directory",
        "-k": "--keep-subject",
        "-s": "--signoff",
        "-q": "--quiet",
    },
    "branch": {"-a": "--all", "-r": "--remotes", "-v": "--verbose"},
    "remote": {"-v": "--verbose"},
    "worktree": {"-v": "--verbose"},
    "ls-remote": {"-q": "--quiet"},
    "update-index": {"-q": "--quiet"},
    # log: -p normalizes to --patch so the BRC re-review delta command
    # (`git log <sha>..HEAD --not origin/<base> -p`) matches the allowlist. #2905
    "log": {"-p": "--patch"},
    # Subcommands with no short-flag normalization needed (included for completeness):
    "status": {},
    "diff": {},
    "show": {},
    "rev-parse": {},
    "ls-tree": {},
    "describe": {},
    "config": {},
    "merge-base": {},
    "diff-tree": {},
    "reflog": {},
    "cat-file": {},
}


# =============================================================================
# Branch Isolation (Worktree Sessions)
# =============================================================================

# Flags that indicate a checkout is a file restore, not a branch switch
_CHECKOUT_FILE_FLAGS = {"--ours", "--theirs", "--merge"}
