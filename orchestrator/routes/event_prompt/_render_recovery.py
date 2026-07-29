"""Worktree-recovery section (#3684).

Renders WHERE the work went when this spawn's worktree re-attach hard-reset
the tree and moved unpushed commits onto an ``egg/recovered/...`` ref. The
notices arrive via the ``EGG_WORKTREE_RECOVERY`` pod env, injected by
``kubernetes_spawner._events.spawn_event_job`` only on the spawn that
follows such a discard (never on ordinary spawns).

The salvage has been reliable since #3639/#3644; the failure this section
exists for is purely one of communication. In the #3684 incident a coder
lost 8 commits / 3072 insertions to a re-attach reset, correctly diagnosed
that its files were gone, and — with no idea the work had been preserved —
began re-implementing all of it. Restoring would have been a two-command
fast-forward. So this section leads the prompt, names the ref and the exact
restore command, and says "preserved", never "discarded".
"""

from __future__ import annotations

from typing import Any

# Renderer-side bound on the one free-text notice field. The producer already
# clamps ``salvage_error`` to 400 chars (``_clamp_salvage_error``); this is the
# belt-and-braces half, because the value reaches us through a pod env var and
# the renderer should not depend on the writer having been careful. Slightly
# looser than the producer's cap so a producer-clamped value renders verbatim
# (with its truncation marker) rather than being truncated twice.
_SALVAGE_ERROR_RENDER_MAX_CHARS = 500


def _sanitize_free_text(value: Any) -> str:
    """Flatten remote-controlled text to a single bounded line.

    ``salvage_error`` is ``git push`` stderr, which carries every ``remote:``
    line the server echoed — pre-receive hook output, policy rejection bodies.
    That is text a party other than the orchestrator controls, and it lands in
    a section headed "READ FIRST" (#3689 review). Collapsing all whitespace
    stops it fabricating its own markdown structure (headings, list items, a
    fenced block that swallows the instructions below it), and the cap bounds
    the blast radius on a notice that skipped the producer-side clamp.
    """
    if value is None:
        return ""
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= _SALVAGE_ERROR_RENDER_MAX_CHARS:
        return collapsed
    return collapsed[:_SALVAGE_ERROR_RENDER_MAX_CHARS] + "..."


def _render_recovery_section(recovery: list[dict[str, Any]] | None) -> str:
    """Render the #3684 worktree-recovery notice, or ``""`` when absent.

    ``recovery`` is the decoded ``EGG_WORKTREE_RECOVERY`` JSON: a list of
    per-repo notice dicts with ``repo``, ``recovery_ref``, ``tip_sha``,
    ``reset_to``, ``n_commits``, ``fast_forward``, ``worktree_id``,
    ``wip_commit``, ``wip_files``, ``wip_partial`` and ``salvage_error``.
    Every field is optional at this layer: the spawner degrades an oversized
    notice to ``repo`` / ``recovery_ref`` / ``tip_sha`` / ``reset_to`` /
    ``fast_forward`` rather than dropping it, so the renderer must produce a
    useful section from that subset alone.

    A notice whose ``recovery_ref`` is ``None`` is a salvage FAILURE, not a
    no-op: the commits are reachable from no ref and survive at best in the
    predecessor worktree's object store until gc, so that arm asks for an
    operator instead of naming a fetch. It is rendered with the same
    prominence — the case with no ref is the case where silently re-deriving
    costs the most. "At best" is load-bearing: the notice is appended before
    the re-attach's ``reset --hard``, and a reset failure sends the caller to
    create-with-retry, so the worktree the successor is running in may not be
    the one holding the tip. The rendered text names the worktree by id and
    says so, rather than asserting "this worktree" (#3689 review).
    """
    if not recovery:
        return ""
    entries = [n for n in recovery if isinstance(n, dict)]
    if not entries:
        return ""

    lines: list[str] = [
        "## READ FIRST: your previous session's work was PRESERVED, not lost",
        "",
        "Before this invocation the orchestrator re-attached your worktree and "
        "hard-reset it to the branch tip. Files your previous session wrote are "
        "NOT in the working tree any more. **They were saved first.** Do not "
        "conclude from an empty diff, a missing file, or a `git log` that shows "
        "none of your commits that the work is gone and has to be redone — that "
        "conclusion is wrong here, and acting on it costs the entire session "
        "(#3684).",
        "",
    ]
    for notice in entries:
        repo = str(notice.get("repo") or "the repo")
        ref = notice.get("recovery_ref")
        tip = str(notice.get("tip_sha") or "")
        reset_to = str(notice.get("reset_to") or "")
        n_commits = notice.get("n_commits")
        count = f"{n_commits} commit(s)" if isinstance(n_commits, int) else "commits"
        if ref:
            lines.append(
                f"- **{repo}**: {count} preserved on remote ref `{ref}` "
                f"(tip `{tip}`). Your worktree is now at `{reset_to}`."
            )
            if notice.get("fast_forward"):
                lines.append(
                    f"  - Restore: `git fetch origin {ref}` then "
                    f"`git merge --ff-only {tip}`. This is a pure fast-forward "
                    "from where you are now — it replays every one of those "
                    "commits and loses nothing."
                )
            else:
                lines.append(
                    f"  - Read it: `git fetch origin {ref}` then "
                    f"`git log --oneline {reset_to}..{tip}`. Take what you need "
                    f"with `git cherry-pick {reset_to}..{tip}`. The ref has "
                    "diverged from your current HEAD, so no fast-forward is "
                    "available."
                )
            lines.append(
                "  - Do NOT `git reset --hard` onto it: the gateway rejects "
                "off-lineage resets in pipeline sessions with a 403, and a "
                "recovery tip is a descendant of your HEAD, never an ancestor."
            )
        else:
            error = _sanitize_free_text(notice.get("salvage_error")) or "unknown error"
            worktree_id = str(notice.get("worktree_id") or "").strip()
            where = f"worktree `{worktree_id}`" if worktree_id else "the predecessor worktree"
            lines.append(
                f"- **{repo}**: {count} were removed from the tree and the "
                f"automatic salvage push FAILED ({error}). Tip `{tip}` is "
                f"reachable from no ref; it may still be in the local git "
                f"object store of {where} until gc."
            )
            lines.append(
                "  - Do NOT start re-deriving the work. Report this and ask an "
                f"operator to recover `{tip}` from {where} (`git reflog`) "
                "first — re-deriving destroys the reflog window that recovery "
                "depends on. Note the worktree you are running in now may not "
                "be that one: a failed re-attach falls back to a fresh "
                "worktree, whose object store does not carry the tip."
            )
        wip_commit = notice.get("wip_commit")
        if wip_commit:
            wip_files = notice.get("wip_files")
            size = (
                f"{wip_files} file(s) of uncommitted work"
                if isinstance(wip_files, int)
                else "uncommitted work"
            )
            lines.append(
                f"  - Commit `{wip_commit}` in that set is an AUTOMATIC "
                f"snapshot of {size} your previous session had not committed. "
                "Treat it as a WIP checkpoint to review, not as work you "
                "already proposed."
            )
            if notice.get("wip_partial"):
                lines.append(
                    "  - WARNING: that snapshot may be INCOMPLETE — `git add "
                    "-A` did not finish cleanly while taking it, so files the "
                    "previous tree held may be missing from it."
                )
    lines.extend(
        [
            "",
            "Recover the work FIRST, then handle the event below. The bus also "
            "carries this as a STATUS message with the same refs in its "
            "metadata, readable via ``mcp__brc__read_peer_artifact``.",
            "",
        ]
    )
    return "\n".join(lines)
