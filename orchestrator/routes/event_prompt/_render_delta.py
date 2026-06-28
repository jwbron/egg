"""Per-producer re-review delta sections — inline diff and JIT-pull pointer.

Two renderers for the per-producer ``git log`` re-review scope:
:func:`_render_producer_delta_section` INLINES the full diff (legacy
full-context path) and :func:`_render_delta_pointer_section` emits the
pull recipe as a JIT-pull POINTER (#3200 queryable-environment path).
The inline renderer is kept byte-for-byte so slice-9's feature flag
preserves the OFF path. AST-identical to the pre-split definitions —
pure refactor (#3312 slice-6).
"""

from __future__ import annotations

from typing import Any


def _render_producer_delta_section(
    git_log_delta: list[dict[str, Any]] | None,
    base_branch: str,
) -> tuple[str, int]:
    """Render the per-producer ``git log`` re-review-scope block.

    Each entry is a dict with keys ``producer``, ``last_reviewed_commit_sha``,
    ``proposal_commit_sha``, and ``delta``. The command form is rendered
    verbatim so the agent sees the exact scope (architect plan
    acceptance: "git-log delta command is emitted verbatim with the
    per-producer ``last_reviewed_commit_sha`` substituted in"); the
    rendered diff follows so the agent can audit the full change as a
    fresh review. ``proposal_commit_sha`` is used as the range end-ref
    (#3076) so the delta is scoped to the producer's pushed work
    instead of the reviewer's own HEAD; legacy payloads without it fall
    back to ``HEAD`` and the rendered caution.

    Returns ``(section_markdown, total_delta_bytes)`` so the caller can
    measure the delta separately from the envelope budget.
    """
    if not git_log_delta:
        return "", 0

    lines: list[str] = [
        "## Per-producer re-review delta",
        "",
        "For each assigned producer below, the wrapper has run the FULL "
        "delta-scoping command from the producer's "
        "``last_reviewed_commit_sha`` (stored in your durable BRC memory) "
        "to ``HEAD`` of the producer's branch, EXCLUDING commits already "
        "on the base branch. Audit the diff as a fresh review per "
        "``shared/prompts/REVIEWER-SYNC.md``: the named-blockers from "
        "your prior NACK MUST be addressed, AND any new findings the "
        "delta introduces are in scope. Both passes must succeed to ACK.",
        "",
    ]

    total_delta_bytes = 0
    for entry in git_log_delta:
        producer = str(entry.get("producer") or "(unknown)").strip()
        sha = str(entry.get("last_reviewed_commit_sha") or "").strip()
        proposal_sha = str(entry.get("proposal_commit_sha") or "").strip()
        delta = entry.get("delta") or ""
        if not isinstance(delta, str):
            delta = str(delta)
        # Command is emitted verbatim — the per-producer
        # ``last_reviewed_commit_sha`` and the proposal endpoint
        # substituted in so the agent can cross-check the scope against
        # the orchestrator's stored values. ``end_ref`` is the
        # producer's proposed commit when the payload carries one
        # (#3076); ``HEAD`` only on legacy payloads.
        cmd_sha = sha if sha else "<no prior review — full branch history>"
        end_ref = proposal_sha or "HEAD"
        cmd = f"git log {cmd_sha}..{end_ref} --not origin/{base_branch} -p"
        if delta.strip():
            delta_rendered = delta
        elif proposal_sha:
            delta_rendered = "(no commits in range — re-review is a no-op)"
        else:
            # Empty delta against the reviewer's own HEAD is NOT
            # evidence the producer didn't revise: per-role worktrees
            # mean the reviewer's HEAD never contains the producer's
            # commits (#3076 — the "re-review delta is empty" phantom
            # NACK). Only trust an empty range when it was scoped to
            # the producer's proposal SHA. When the wrapper's
            # ``sync_to_proposals`` could not merge the producer's
            # commit into your worktree (``unresolvable`` or
            # ``merge-failed``), #3077 slice-1 task-1-1 prepends a
            # ``worktree NOT synced to <sha>`` banner at the very top
            # of this prompt — check for it and re-run the
            # ``git log`` command rendered above against the producer's
            # branch directly rather than trusting your local diff.
            delta_rendered = (
                "(no commits in range — CAUTION: this range ended at YOUR "
                "worktree's HEAD, which does not contain the producer's "
                "commits. An empty delta here is NOT evidence the producer "
                "didn't revise. Check the TOP of this prompt for a "
                "``worktree NOT synced to <sha>`` banner (#3077 slice-1): "
                "when present, the wrapper could not sync your worktree to "
                "the producer's commit — re-run the ``git log`` command "
                "shown in the ``Re-review scope`` line above against the "
                "producer's branch (e.g. "
                "`git log <producer-branch-or-sha> --not "
                f"origin/{base_branch} -p`) rather than trusting your "
                "local diff. If no banner is present, the same fallback "
                "applies before issuing a verdict.)"
            )
        lines.extend(
            [
                f"### Producer: ``{producer}``",
                "",
                f"- last_reviewed_commit_sha: `{sha or '-'}`",
                f"- proposal_commit_sha: `{proposal_sha or '-'}`",
                "- Re-review scope (executed by the wrapper):",
                f"  `{cmd}`",
                "",
                "Delta:",
                "```diff",
                delta_rendered,
                "```",
                "",
            ]
        )
        total_delta_bytes += len(delta.encode("utf-8"))

    return "\n".join(lines), total_delta_bytes


def _render_delta_pointer_section(
    git_log_delta: list[dict[str, Any]] | None,
    base_branch: str,
    pipeline_id: str = "",
) -> str:
    """Render the re-review delta as JIT-pull POINTERS (#3200 slice-5).

    The new-discipline counterpart of :func:`_render_producer_delta_section`,
    which INLINES the full per-producer ``git log`` diff (potentially
    hundreds of KB) into every one-shot prompt. Under the #3200 context
    discipline that bulk moves into the *queryable environment*: the
    prompt carries only the exact ``git log
    <last_reviewed>..<proposal> --not origin/<base> -p`` recipe (scoped
    by the #3189 anchors already in the payload) plus the served-read
    handles (``mcp__brc__read_peer_artifact`` /
    ``GET /<pipeline_id>/brc-transcript``), and the agent pulls the diff
    just-in-time only for the producer THIS event names.

    Honest limit (recorded here and in the rendered prose): JIT pull does
    NOT bound the context window — a pulled slice stays resident until the
    next reseed; the slice-6 reseed bounds the window, the pull only
    lowers the resident root cost and makes the reseed re-pull-able.

    Self-contained (no ``egg_agent`` import) because this module runs
    standalone via the wrapper bash — the same constraint that forces
    :func:`_issue_anchor_fallback` to duplicate
    ``compose_task_description``. The canonical renderer lives in
    ``egg_agent.queryable_env``; the wording is kept in sync deliberately.

    ADDITIVE: :func:`_render_producer_delta_section` is left byte-for-byte
    unchanged so slice-9's feature flag preserves the OFF (inline) path.
    """
    if not git_log_delta:
        return ""
    pid = (pipeline_id or "<pipeline_id>").strip() or "<pipeline_id>"
    base_branch = (base_branch or "main").strip() or "main"
    lines: list[str] = [
        "## Per-producer re-review delta (pull on demand)",
        "",
        "The full diff is NOT inlined. Pull it just-in-time with the "
        "exact recipe below, only for the producer(s) THIS event names:",
        "",
    ]
    for entry in sorted(git_log_delta, key=lambda e: str(e.get("producer") or "")):
        producer = str(entry.get("producer") or "(unknown)").strip() or "(unknown)"
        sha = str(entry.get("last_reviewed_commit_sha") or "").strip()
        proposal_sha = (
            str(entry.get("proposal_commit_sha") or "").strip() or "<proposal_commit_sha>"
        )
        if sha:
            recipe = f"git log {sha}..{proposal_sha} --not origin/{base_branch} -p"
        else:
            recipe = f"git log {proposal_sha} --not origin/{base_branch} -p"
        lines.append(f"### Producer: ``{producer}``")
        lines.append(f"- Pull the delta: ``{recipe}``")
        lines.append("")
    lines.extend(
        [
            "Bulk BRC history and peer-artifact content are also NOT inlined — pull on demand:",
            "",
            "- Peer artifacts + message transcript: ``mcp__brc__read_peer_artifact``.",
            f"- Live in-flight transcript: ``GET /{pid}/brc-transcript?"
            "phase=implement&role=<your-role>``.",
            "",
            "Honest limit: pulling the delta/transcript does NOT bound "
            "your context window — a pulled slice stays resident until the "
            "next reseed. The reseed bounds the window; the pull only "
            "lowers the resident root cost and makes the reseed "
            "re-pull-able. Pull only what THIS event needs.",
            "",
        ]
    )
    return "\n".join(lines)
