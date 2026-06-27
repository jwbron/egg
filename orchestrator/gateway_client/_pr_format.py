"""PR-body formatting helpers (titles, sections, slugs) (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

import re
from typing import Any

# Trailing characters trimmed from a truncated PR title.
_TITLE_TRAILING_PUNCT = " \t\r\n.,;:!?-+/\\|&*=~^<>\"'()[]{}"


def _derive_program_slug(pipeline_id: str, max_len: int = 18) -> str:
    """Derive a short program slug from ``pipeline_id`` for slice PR titles.

    ``issue-<N>[-v<K>]`` collapses to ``issue-<N>`` (drops version suffix
    so reviewers see a stable identifier across pipeline re-runs).
    ``pipeline-<hash>`` keeps the prefix and truncates the hash to fit.
    Any other shape is truncated as-is.

    ``max_len`` keeps the slug short so the slice-PR title still has room
    for the position marker (``[slice-N/M]``) and the slice subject inside
    the 70-char title cap. Worst-case budget: a hash-id pipeline at slice
    99/100 leaves roughly 30 chars for the subject after the slug
    (``[pipeline-f4c7d780ab][slice-99/100] ``) — tight, so the subject is
    what gets truncated first. ``issue-<N>`` pipelines are unaffected
    because the slug collapses to ``issue-<N>`` regardless of ``max_len``.
    """
    if not pipeline_id or not pipeline_id.strip():
        return "pipeline"
    pid = pipeline_id.strip()
    issue_match = re.match(r"^(issue-\d+)(?:-v\d+)?", pid)
    if issue_match:
        return issue_match.group(1)
    if len(pid) <= max_len:
        return pid
    return pid[:max_len]


def _format_position_marker(
    slice_id: str,
    slice_index: int | None,
    slice_count: int | None,
) -> str:
    """Return ``slice-N/M`` (or ``slice_id`` when index/count aren't supplied).

    Pre-#2777 cq-6 the terminal slice received a dedicated ``merge-gate``
    marker so its title was distinguishable from a hypothetical sibling
    program-level rollup PR. Under cq-4 the merge gate moved to the
    up-front context PR (``egg/<id>/work → main``), so every slice PR
    — including the last-to-merge slice — uses the uniform
    ``slice-N/M`` shape; the ``[merge-gate]`` marker is gone.
    """
    if slice_index is not None and slice_count is not None and slice_count >= 1:
        return f"slice-{slice_index}/{slice_count}"
    return slice_id


def _format_slice_title(program_slug: str, position_marker: str, subject: str) -> str:
    """Compose ``[<slug>][<position>] <subject>``."""
    return f"[{program_slug}][{position_marker}] {subject}".strip()


def _truncate_title(title: str, max_len: int = 70) -> str:
    """Truncate ``title`` to ``max_len`` chars at a word boundary.

    Replaces the bare ``title[:67] + "..."`` cut that produced mid-word
    titles like ``claim-che...`` (#3115). When a space exists in the
    back half of the truncated prefix the cut moves to it, so the
    ellipsis follows a whole word; otherwise (one giant token) the
    hard cut is kept. Any trailing punctuation / symbols are stripped
    before ``...`` is appended so we don't produce results like
    ``library +...`` (the ``...`` itself is the only trailing
    punctuation we want).
    """
    if len(title) <= max_len:
        return title
    # Guard against degenerate ``max_len`` — without this,
    # ``_truncate_title("a a", 2)`` returns ``"a..."`` (length 4 > 2).
    if max_len <= 3:
        return title[:max_len]
    prefix = title[: max_len - 3]
    space = prefix.rfind(" ")
    if space > (max_len - 3) // 2:
        prefix = prefix[:space]
    return prefix.rstrip(_TITLE_TRAILING_PUNCT) + "..."


def _first_sentence(text: str, max_len: int = 120) -> str:
    """Return the first sentence of ``text``, capped at ``max_len`` chars.

    Used as the slice-PR program blurb — meant to be a 1-line hook, not
    a paragraph. The blurb ends at whichever boundary comes first:

    * the first ``.``/``!``/``?`` followed by whitespace or end-of-string;
    * the first newline (so a description that opens with a markdown
      bullet list or a header doesn't bleed into the blurb);
    * ``max_len`` chars (truncated with a trailing ``...``).

    Returns ``""`` when ``text`` is empty / whitespace-only.
    """
    if not text:
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    # Take everything up to the first newline (in the original string,
    # before collapsing whitespace) so a list / header on the second
    # line is excluded from the blurb.
    first_line = stripped.split("\n", 1)[0]
    collapsed = " ".join(first_line.split())
    if not collapsed:
        return ""
    match = re.search(r"[.!?](?:\s|$)", collapsed)
    if match:
        end = match.start() + 1
        sentence = collapsed[:end]
    else:
        sentence = collapsed
    if len(sentence) > max_len:
        sentence = sentence[: max_len - 3].rstrip() + "..."
    return sentence


def _append_task_bullets(
    body_lines: list[str],
    slice_tasks: list[dict[str, Any]] | None,
    *,
    header: str | None = None,
) -> None:
    """Append task bullets — full descriptions + acceptance criteria.

    Drops the pre-#2745 300-char truncation. Acceptance criteria render
    as a nested bullet when present. Whitespace is collapsed to a single
    line so list rendering on GitHub stays stable.
    """
    if not slice_tasks:
        return
    if header:
        body_lines.append("")
        body_lines.append(header)
    for task in slice_tasks:
        desc = task.get("description") or task.get("id") or ""
        desc = " ".join(str(desc).split())
        task_id = task.get("id") or ""
        bullet_prefix = f"- {task_id}: " if task_id else "- "
        body_lines.append(f"{bullet_prefix}{desc}")
        ac = task.get("acceptance_criteria") or ""
        ac = " ".join(str(ac).split())
        if ac:
            body_lines.append(f"  - Acceptance criteria: {ac}")


def _append_this_slice_section(
    body_lines: list[str],
    slice_name: str,
    slice_files_affected: list[str] | None,
    slice_tasks: list[dict[str, Any]] | None,
) -> None:
    """Render the ``## This slice`` block: subject + files + tasks.

    ``slice_files_affected`` is treated as already deduplicated /
    empty-filtered by the caller (``_run_one_slice_inner`` is the only
    production caller and does this work under the contract-state lock).
    """
    body_lines.append("## This slice")
    body_lines.append("")
    body_lines.append(slice_name)
    if slice_files_affected:
        body_lines.append("")
        body_lines.append("Files affected:")
        for path in slice_files_affected:
            body_lines.append(f"- `{path}`")
    if slice_tasks:
        # Collapse the full task dump behind a <details> fold (#3115).
        # Task descriptions are planning-consensus prose — useful for
        # traceability, unreadable as the body's main content. The
        # blank line after </summary> is required for GitHub to render
        # the markdown list inside the fold.
        body_lines.append("")
        body_lines.append("<details>")
        body_lines.append(f"<summary>Tasks ({len(slice_tasks)}) + acceptance criteria</summary>")
        body_lines.append("")
        _append_task_bullets(body_lines, slice_tasks, header=None)
        body_lines.append("")
        body_lines.append("</details>")
    body_lines.append("")


def _append_diff_summary_section(
    body_lines: list[str],
    diffstat: str | None,
    commit_subjects: list[str] | None,
    *,
    max_commits: int = 20,
) -> None:
    """Render the ``## What's in this PR`` block from real git state (#3115).

    ``diffstat`` and ``commit_subjects`` are computed by the caller
    (``_build_slice_diff_summary`` in the slice run loop) from the
    pushed integration branch, so unlike the plan-derived task list
    this section reflects what the branch actually contains. Skipped
    entirely when neither input is available (diff summary is
    best-effort — a fetch failure must not block PR creation).
    """
    if not diffstat and not commit_subjects:
        return
    body_lines.append("## What's in this PR")
    body_lines.append("")
    if commit_subjects:
        shown = commit_subjects[:max_commits]
        body_lines.append(f"Commits ({len(commit_subjects)}):")
        for subject in shown:
            body_lines.append(f"- {subject}")
        remainder = len(commit_subjects) - len(shown)
        if remainder > 0:
            body_lines.append(f"- … and {remainder} more")
        body_lines.append("")
    if diffstat:
        body_lines.append("```text")
        body_lines.append(diffstat.rstrip("\n"))
        body_lines.append("```")
        body_lines.append("")


def _format_stack_block(
    *,
    pipeline_id: str,
    slice_id: str,
    slice_index: int | None,
    slice_count: int | None,
    base_branch: str,
    context_pr_number: int | None,
) -> list[str]:
    r"""Render the ``## Stack`` footer with base PR + position pointers.

    Replaces the pre-#2745 trailing ``"Slice X of pipeline Y. Stacked
    on top of \`base\`."`` line with a structured block so reviewers
    can navigate the stack without leaving the PR.

    A ``Parent PR:`` line would also be useful here, but slice PR numbers
    aren't persisted on the contract yet, so the parent PR # isn't
    available at slice-PR-creation time. Tracked separately.

    Pre-#2777 cq-6 the terminal slice received a distinct
    ``merge-gate (slice N of M)`` position; under cq-4 the merge gate is
    the up-front context PR (``egg/<id>/work → main``), so all slices
    use the uniform ``slice N of M`` shape.
    """
    lines: list[str] = ["## Stack", ""]
    if slice_index is not None and slice_count is not None and slice_count >= 1:
        position = f"slice {slice_index} of {slice_count}"
    else:
        position = slice_id
    lines.append(f"- Position: {position} in pipeline `{pipeline_id}`")
    if context_pr_number is not None and context_pr_number >= 1:
        lines.append(f"- Base PR: #{context_pr_number}")
    lines.append(f"- Stacked on top of `{base_branch}`")
    # The legacy plain-text footer ("Slice <id> of pipeline <id>. Stacked
    # on top of `<base>`.") was dropped in #3115 — a repo-wide search
    # found no parser consuming it, only the writer and its tests, and it
    # duplicated every fact in the structured block above.
    return lines
