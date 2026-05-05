"""Render the "Pre-merge Obligations" PR-body section from ``DeferredAction``.

Shared between the legacy ``_auto_create_pr`` path
(``orchestrator/routes/pipelines.py::_build_pre_merge_obligations_section``)
and the slice-DAG umbrella PR path
(``orchestrator/gateway_client.py::create_slice_pr``) so both surface the
same conditional-ACK obligations under the same merge-blocking banner
(#2354).

Pure with respect to the obligation list: no orchestrator runtime or
``peer_consensus`` tracker access lives here. Callers that want the
tracker fallback (the legacy path) wrap this module's
:func:`render_obligations_section` with their own collection logic.
"""

from __future__ import annotations

from typing import Any


def normalize_deferred_actions(
    deferred_actions: list[Any] | None,
) -> list[dict[str, str]]:
    """Normalize ``DeferredAction`` objects (or legacy strings) into dicts.

    Returns a list of ``{reviewer, condition, resolved_in_diff}`` dicts.
    Whitespace-only conditions are dropped — the renderer would emit an
    empty bullet otherwise. Legacy ``str`` entries (pre-#2336 contract
    shape) are parsed with the same ``"reviewer: condition"`` split the
    ``PRMetadata.deferred_actions`` field validator uses, so callers that
    skip the validator (unit tests building inputs by hand) still
    produce the expected shape.
    """
    if not deferred_actions:
        return []

    normalized: list[dict[str, str]] = []
    for entry in deferred_actions:
        reviewer = getattr(entry, "reviewer", None)
        condition = getattr(entry, "condition", None)
        resolved = getattr(entry, "resolved_in_diff", None)
        if condition is None and isinstance(entry, str):
            text = entry.strip()
            if not text:
                continue
            head, sep, tail = text.partition(": ")
            if sep and tail.strip():
                reviewer, condition = head.strip(), tail.strip()
            else:
                reviewer, condition = "", text
            resolved = ""
        condition_text = (condition or "").strip()
        if not condition_text:
            continue
        normalized.append(
            {
                "reviewer": (reviewer or "").strip(),
                "condition": condition_text,
                "resolved_in_diff": (resolved or "").strip(),
            }
        )
    return normalized


def _format_obligation_bullet(
    obligation: dict[str, str],
    resolved: bool,
) -> list[str]:
    """Format a single obligation as a markdown bullet (multi-line aware)."""
    reviewer = obligation["reviewer"] or "unknown"
    # ``strip()`` before ``splitlines()`` so a leading/trailing newline doesn't
    # produce an empty first line ("- **reviewer** — " with nothing after the
    # em-dash). The collector filters whitespace-only conditions, but a
    # ``"\nreal text"`` value would otherwise slip through with an empty
    # first line (#2336 review).
    condition = obligation["condition"].strip()
    first, *rest = condition.splitlines()
    bullet = f"- **{reviewer}** — {first}"
    lines = [bullet]
    for extra in rest:
        lines.append(f"  {extra}")
    if resolved and obligation["resolved_in_diff"]:
        # Bare SHA (no backticks) so GitHub auto-links it to the commit page
        # in the rendered PR body — code-span text is not auto-linked.
        lines.append(f"  - Resolved in {obligation['resolved_in_diff']}")
    return lines


def render_obligations_section(
    deferred_actions: list[Any] | None,
) -> str:
    """Compose the PR-body Pre-merge / Resolved obligations markdown.

    ``deferred_actions`` may contain ``DeferredAction`` Pydantic objects
    or legacy ``str`` entries (handled defensively by
    :func:`normalize_deferred_actions`). Returns the empty string when no
    obligations remain after normalization, so callers can
    unconditionally append the result to a PR body.

    Each obligation is classified as **open** or **resolved** (#2336):

    * Open obligations (no ``resolved_in_diff``) render under a merge-blocking
      "Pre-merge Obligations" banner — a human must act before merging.
    * Resolved obligations (the reviewer marked them satisfied within the same
      PR's diff) render under a "Resolved within this PR" subsection with a
      pointer to the satisfying commit. They do not block merge.
    """
    return render_obligations_section_from_normalized(normalize_deferred_actions(deferred_actions))


def render_obligations_section_from_normalized(
    obligations: list[dict[str, str]],
) -> str:
    """Compose the markdown from already-normalized obligation dicts.

    Used by callers (the legacy ``_build_pre_merge_obligations_section``
    in ``routes/pipelines.py``) that collect obligations from multiple
    sources — contract list + live consensus tracker — and need to merge
    them before rendering.
    """
    if not obligations:
        return ""

    open_obligations = [o for o in obligations if not o["resolved_in_diff"]]
    resolved_obligations = [o for o in obligations if o["resolved_in_diff"]]

    sections: list[str] = []

    if open_obligations:
        lines: list[str] = [
            "## ⚠️ Pre-merge Obligations",
            "",
            "The reviewers below issued a **conditional ACK** — the work is "
            "approved, but a human must perform the listed action before "
            "merging. Do **not** merge this PR until every obligation is "
            "complete.",
            "",
        ]
        for o in open_obligations:
            lines.extend(_format_obligation_bullet(o, resolved=False))
        sections.append("\n".join(lines))

    if resolved_obligations:
        # When the only obligations on the PR are already-satisfied ones, this
        # subsection stands on its own — there's no "do not merge" banner to
        # contradict (#2336). Reviewers still get a record of what was
        # promised and what diff resolved it.
        lines = [
            "## ✅ Resolved within this PR",
            "",
            "The reviewers below issued a **conditional ACK** and later "
            "marked the obligation satisfied within this PR's diff. Listed "
            "for the audit trail; no merge action required.",
            "",
        ]
        for o in resolved_obligations:
            lines.extend(_format_obligation_bullet(o, resolved=True))
        sections.append("\n".join(lines))

    return "\n\n".join(sections)
