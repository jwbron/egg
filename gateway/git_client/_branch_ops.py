"""Branch-switch / reset-target detection + ``rebase --onto`` builder.

Extracted verbatim from the pre-split ``gateway/git_client.py``
(#3312 slice-11). AST-identical to the originals — pure refactor.
"""

import re

from ._validation import validate_git_args


def is_branch_switch(operation: str, args: list[str]) -> bool:
    """Detect if a checkout/switch invocation changes branches.

    Returns True when the command would switch the active branch (e.g.
    ``git checkout other-branch``, ``git checkout -b new``).  Returns False
    for file-level operations (``git checkout -- file.txt``,
    ``git checkout HEAD -- file``, ``git checkout main -- file``).

    Only meaningful for ``checkout`` and ``switch`` operations; returns
    False for everything else.

    The heuristic:
    * If ``--`` separator is present, everything after it is a pathspec and
      anything before it is a commit-ish source for the file restore
      (``HEAD``, ``HEAD~1``, a sha, a branch name) — NOT a branch switch.
    * ``switch`` always operates on branches, so any invocation of
      ``switch`` is considered a branch switch.
    * ``checkout`` with ``-b``/``-B`` or ``--orphan`` is a branch switch.
    * ``checkout`` with a positional arg (no ``--`` before it) and without
      ``-p``/``--patch`` is a branch switch.
    """
    if operation not in ("checkout", "switch"):
        return False

    # git switch always targets branches
    if operation == "switch":
        return True

    # Parse the checkout args
    has_double_dash = "--" in args
    positional_before_dd: list[str] = []
    branch_creating_flags = {"-b", "-B", "--orphan"}
    file_flags = {"-p", "--patch"}

    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-"):
            if arg in branch_creating_flags:
                return True
            if arg in file_flags:
                return False
            continue
        positional_before_dd.append(arg)

    # ``--`` separates a commit-ish source from pathspecs; the form
    # ``checkout [<tree-ish>] -- <pathspec>`` restores files without
    # switching branches, regardless of whether a tree-ish is present.
    if has_double_dash:
        return False

    # No ``--``: any positional arg is a branch/ref to switch to.
    if positional_before_dd:
        return True

    # No positional args, no ``--`` → bare checkout, no-op.
    return False


def extract_reset_target_ref(args: list[str]) -> str | None:
    """Return the target commit ref for a ``git reset`` that would move HEAD.

    ``git reset`` has two distinct forms:
    * ``git reset [<mode>] [<commit>]`` — moves HEAD (and optionally index/
      worktree, depending on mode) to ``<commit>``.
    * ``git reset [<commit>] -- <pathspec>...`` or
      ``git reset <commit> <pathspec>...`` — index-only reset for the listed
      paths; HEAD does not move.

    Only the first form is relevant to the pipeline branch-lock: it can land
    HEAD on a commit that is not on the assigned-branch lineage. This helper
    returns the target ref for that form and ``None`` otherwise (so the caller
    can skip the ancestry check entirely for path-mode resets).

    The returned ref is purely syntactic — the caller is responsible for
    resolving it (e.g., via ``git merge-base --is-ancestor``) to decide
    whether the reset is on-lineage.

    Args:
        args: The validated/normalized argument list for ``git reset``.

    Returns:
        The target ref string if the invocation would move HEAD, else ``None``.
    """
    if "--" in args:
        return None
    positional = [arg for arg in args if not arg.startswith("-")]
    if len(positional) != 1:
        return None
    return positional[0]


# ---------------------------------------------------------------------------
# #2137 — narrow ``rebase --onto`` helper for the stacked-PR reconciler.
#
# The ``rebase --onto`` invocation is already on the per-agent allowlist
# (``ALLOWED_GIT_OPERATIONS["rebase"]["allowed_flags"]`` includes
# ``--onto``), so authorised agents can already drive the operation
# through the existing ``/git`` endpoint. This helper is a thin
# typed-wrapper that constructs the canonical argument shape
#
#     git rebase --onto <new_base> <old_base> <branch>
#
# and validates it against the same allowlist plumbing — explicitly
# rejecting any extra flags (e.g. ``--strategy-option=ours``) that an
# attacker-controlled caller might try to slip in. It does NOT add a
# new privileged orchestrator-role endpoint (per refine-phase
# decision-15) — the reconciler caller authenticates as the existing
# low-privilege agent identity that already has rebase capability.
#
# Returns ``(args, ok, error)`` so the orchestrator can submit ``args``
# through the standard ``/git`` validate-and-execute path; the test
# suite asserts the shape and that any extra flag is rejected.
# ---------------------------------------------------------------------------


def build_rebase_onto_args(
    branch: str, new_base: str, old_base: str
) -> tuple[list[str], bool, str]:
    """Construct the canonical ``rebase --onto`` argv for the reconciler.

    Args:
        branch: The child branch to rebase.
        new_base: The new base to land the branch on top of.
        old_base: The old base whose history should be excluded from
            the rebase (the part of ``branch`` between ``old_base`` and
            its tip is what gets replayed onto ``new_base``).

    Returns:
        ``(args, ok, error)``. When ``ok`` is True, ``args`` is the
        argument list to pass to the gateway's ``/git`` endpoint.
        When ``ok`` is False, ``error`` describes why (input was
        rejected by the allowlist validator).

    Each input is wrapped through :func:`validate_git_args` to ensure
    the shape is identical to what an agent-driven invocation would
    produce — no new code path is introduced. This keeps the audit
    surface unchanged.
    """
    if not isinstance(branch, str) or not branch.strip():
        return [], False, "branch must be a non-empty string"
    if not isinstance(new_base, str) or not new_base.strip():
        return [], False, "new_base must be a non-empty string"
    if not isinstance(old_base, str) or not old_base.strip():
        return [], False, "old_base must be a non-empty string"

    # Defense-in-depth shape check — refs must look like git refs, not
    # like rebase flags. ``validate_git_args`` accepts any token in
    # ``rebase``'s allowlist (``--abort`` / ``--continue`` / ``-i``
    # etc.) regardless of position, so a caller-supplied
    # ``branch="--abort"`` would otherwise produce
    # ``git rebase --onto X Y --abort`` which behaves wildly
    # differently from the intended canonical shape. This guard
    # rejects any input starting with ``-`` or containing
    # whitespace / NUL.
    _REF_RE = re.compile(r"^[A-Za-z0-9._/+-][A-Za-z0-9._/+-]*$")
    for label, value in (("branch", branch), ("new_base", new_base), ("old_base", old_base)):
        v = value.strip()
        if v.startswith("-"):
            return [], False, f"{label} must not start with '-' (rejected flag-shaped ref: {v!r})"
        if any(ch.isspace() or ch == "\x00" for ch in v):
            return (
                [],
                False,
                f"{label} must not contain whitespace or NUL (rejected: {v!r})",
            )
        if not _REF_RE.fullmatch(v):
            return (
                [],
                False,
                f"{label} must look like a git ref (alnum + . _ / + -); got {v!r}",
            )

    # Construct the canonical shape. ``--autostash`` is prepended so the
    # rebase proceeds against a worktree carrying uncommitted
    # ``.egg-state/agent-outputs/`` residue (BRC memory writes left
    # uncommitted because post-agent auto-commit is disabled). Without
    # it ``git rebase`` refuses with ``cannot rebase: You have unstaged
    # changes`` even on conflict-free content (#3245) — the same refusal
    # #2714 fixed on the push-reconcile rebase path. We do NOT accept any
    # other extra flags — callers that need ``--abort`` / ``--continue``
    # go through the regular agent-driven path.
    args = ["--autostash", "--onto", new_base, old_base, branch]
    ok, err, _ = validate_git_args("rebase", args)
    if not ok:
        return [], False, err
    return args, True, ""
