"""
Pipeline mode helper (issue #1557).

The Jira-epic SDLC pipeline distinguishes four canonical modes
(``ticket``, ``github_issue``, ``epic-fresh``, ``epic-reassess``); the
orchestrator injects the active mode into the sandbox as
``EGG_EPIC_MODE`` so downstream agents can branch on it.

This module is intentionally tiny and dependency-free so callers in
``orchestrator/routes/pipelines.py`` can import it without pulling in
agent-runtime dependencies.
"""

from __future__ import annotations


def derive_pipeline_mode(
    *,
    is_epic: bool,
    pipeline_mode: str | None,
    jira_ticket: str | None,
) -> str:
    """Compute the canonical ``EGG_EPIC_MODE`` value for a pipeline.

    The mapping rule (issue #1557 task-1-1 — canonical):

    - ``is_epic=True`` + ``pipeline_mode='fresh'``    → ``'epic-fresh'``
    - ``is_epic=True`` + ``pipeline_mode='reassess'`` → ``'epic-reassess'``
    - ``is_epic=False`` + ``jira_ticket is not None`` → ``'ticket'``
    - else                                            → ``'github_issue'``

    The orchestrator injects the return value into the sandbox env as
    ``EGG_EPIC_MODE`` so the agent loop and any auxiliary callers see
    the same string.
    """
    if is_epic:
        if pipeline_mode == "fresh":
            return "epic-fresh"
        if pipeline_mode == "reassess":
            return "epic-reassess"
        # Defensive fallback — an epic pipeline whose pipeline_mode
        # didn't resolve at submission shouldn't reach an agent, but
        # if it does, prefer "epic-fresh" so the prompt still has a
        # valid section to render against.
        return "epic-fresh"
    if jira_ticket:
        return "ticket"
    return "github_issue"


__all__ = [
    "derive_pipeline_mode",
]
