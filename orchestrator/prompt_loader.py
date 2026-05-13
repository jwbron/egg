"""
Mode-aware prompt-loading helper (issue #1557).

The Jira-epic SDLC pipeline uses **mode-parameterised** prompt files —
the refiner, task-planner, and applier prompts in
``plugins/refine-plan/skills/refine-plan/agents/`` carry one section
per supported mode (``ticket``, ``github_issue``, ``epic-fresh``,
``epic-reassess``). Per risk_analyst R10 mitigation (b), the
orchestrator strips the non-matching mode blocks **server-side**
before the prompt is sent to the agent so the agent never sees
competing mode branches and the pattern is robust across model
upgrades.

This module is intentionally tiny and dependency-free so callers in
``orchestrator/routes/pipelines.py`` can import it without pulling in
agent-runtime dependencies.

Markup conventions
------------------
A mode block starts with a level-2 header of the form
``## [mode: <mode-name>]`` on its own line. The block extends until
the next level-1 / level-2 header or end-of-file, whichever comes
first. Modes that don't match the active mode are stripped entirely;
the matching mode's block is preserved verbatim with its header line
removed (so the result looks like a single-mode prompt). Headers that
don't match the canonical shape (e.g. ``## [mode: epic-Fresh]``
with mixed case, or a malformed bracket) are left in place
unchanged — the parser intentionally fails open rather than risk
silently dropping content the prompt author intended to keep.
"""

from __future__ import annotations

import re
from typing import Final

# Canonical mode names the prompts may carry. ``ticket`` and
# ``github_issue`` cover the pre-#1557 shapes; ``epic-fresh`` and
# ``epic-reassess`` were added by #1557.
KNOWN_MODES: Final[frozenset[str]] = frozenset(
    {"ticket", "github_issue", "epic-fresh", "epic-reassess"}
)

# Header regex — matches at start-of-line, exact lower-case mode
# names. Captures the active mode in group 1 for the strip pass.
# Anchored with `(?m)` (multi-line) so it can match within a long
# prompt string in one shot.
_MODE_HEADER_RE: Final[re.Pattern[str]] = re.compile(r"(?m)^##\s*\[mode:\s*([a-z0-9_-]+)\s*\]\s*$")

# Used to detect the next "block boundary" — any header at level 1 or
# level 2. We deliberately match more than just mode headers so a non-
# mode level-2 heading (``## Approach``) terminates the active mode's
# scope cleanly.
_HEADER_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(r"(?m)^(#{1,2})\s+\S.*$")


def _looks_like_mode_header(line: str) -> bool:
    """Return True if ``line`` is exactly a ``## [mode: NAME]`` header."""
    return _MODE_HEADER_RE.match(line) is not None


def prep_mode_aware_prompt(prompt_text: str, mode: str | None) -> str:
    """Return ``prompt_text`` with non-matching ``## [mode: X]`` blocks
    stripped (issue #1557 task-1-1).

    Parameters
    ----------
    prompt_text:
        The raw prompt body (e.g. the contents of
        ``plugins/refine-plan/skills/refine-plan/agents/refiner.md``).
    mode:
        Active pipeline mode. When ``None`` or a string outside
        ``KNOWN_MODES``, the prompt is returned unchanged so the call
        site can fall through to the legacy single-mode shape rather
        than silently emptying the prompt.

    Returns
    -------
    str
        The prompt with:
        - blocks under any ``## [mode: X]`` header where ``X != mode``
          removed entirely (header + body, up to the next header at
          level 1 or 2);
        - the matching ``## [mode: <mode>]`` header **line** removed,
          but its body preserved verbatim so downstream rendering
          looks like a single-mode prompt;
        - text outside any mode block preserved verbatim.

    The function is intentionally pure (no I/O) and string-only so
    it can be unit-tested without touching disk.
    """
    if not prompt_text:
        return prompt_text
    if mode is None or mode not in KNOWN_MODES:
        # Unknown / missing mode: don't strip anything. The prompt
        # author can audit the active mode via ``EGG_EPIC_MODE``.
        return prompt_text

    # Find all mode headers + their positions so we can splice.
    lines = prompt_text.splitlines(keepends=True)
    # Build a (line_index, mode_name) list for every mode header.
    headers: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        match = _MODE_HEADER_RE.match(line)
        if match:
            headers.append((idx, match.group(1)))

    if not headers:
        # No mode markup in this prompt — nothing to strip.
        return prompt_text

    # For each mode header, compute the block boundary: the line
    # index where the next level-1 / level-2 header starts (or
    # len(lines) if none).
    boundaries: list[int] = []
    for header_idx, _ in headers:
        next_boundary = len(lines)
        for scan_idx in range(header_idx + 1, len(lines)):
            scan_line = lines[scan_idx]
            if _looks_like_mode_header(scan_line):
                next_boundary = scan_idx
                break
            if _HEADER_BOUNDARY_RE.match(scan_line):
                next_boundary = scan_idx
                break
        boundaries.append(next_boundary)

    # Build the output. Walk the input line-by-line; when we enter a
    # mode block, decide whether to keep / strip based on the mode
    # match. The matching block keeps its body but drops the header
    # line; non-matching blocks drop the whole range.
    keep_ranges: list[tuple[int, int]] = []
    cursor = 0
    for (header_idx, header_mode), block_end in zip(headers, boundaries, strict=True):
        # Preserve everything between the previous cursor and this
        # header.
        if header_idx > cursor:
            keep_ranges.append((cursor, header_idx))
        if header_mode == mode:
            # Drop the header line; keep the body.
            keep_ranges.append((header_idx + 1, block_end))
        # else: drop both header and body entirely.
        cursor = block_end
    if cursor < len(lines):
        keep_ranges.append((cursor, len(lines)))

    chunks: list[str] = []
    for start, end in keep_ranges:
        chunks.extend(lines[start:end])
    return "".join(chunks)


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
    ``EGG_EPIC_MODE`` so the agent loop and the mode-block strip
    helper above see the same string.
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
    "KNOWN_MODES",
    "derive_pipeline_mode",
    "prep_mode_aware_prompt",
]
