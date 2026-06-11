"""Markdown reflow helpers for pipeline-generated PR bodies (#3122).

The planner authors ``pr:`` block fields (``description``, ``test_plan``,
``manual_steps``) and per-slice ``goal`` text as YAML ``|`` literal block
scalars, naturally hard-wrapped at ~75 characters. GitHub renders every
newline in a PR/issue body as a visible line break, so wrapped prose
comes out as a choppy column of short lines instead of paragraphs.

:func:`unwrap_soft_breaks` joins those soft wraps back into paragraphs
while leaving real markdown structure alone. The heuristic is
conservative: when a line *could* be structural, it is never joined.
"""

from __future__ import annotations

import re

# A line that *starts* a markdown block element must never be appended
# to the previous line (joining would swallow the element), and must
# never have the following line appended to it (the element ends at the
# newline). Covers: ATX headings, bullet / ordered list items,
# blockquotes, table rows, fences, thematic breaks and setext-heading
# underlines (``---`` / ``===``), and footnote/link-reference
# definitions (``[label]: ...``).
_BLOCK_MARKER = re.compile(
    r"""^\s{0,3}(
        \#{1,6}(\s|$)          # ATX heading
      | [-*+]\s                # bullet list item
      | \d{1,9}[.)]\s          # ordered list item
      | >                      # blockquote
      | \|                     # table row
      | (`{3,}|~{3,})          # code fence
      | (-\s*){3,}$            # thematic break / setext h2 underline
      | (\*\s*){3,}$           # thematic break (asterisks)
      | (_\s*){3,}$            # thematic break (underscores)
      | =+\s*$                 # setext h1 underline
      | \[[^\]]+\]:            # link-reference definition
    )""",
    re.VERBOSE,
)

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

# Four-plus leading spaces (or a tab) opens an indented code block in
# markdown; those lines are preformatted and must keep their newlines.
_INDENTED_CODE = re.compile(r"^(?: {4,}|\t)")


def _is_prose(line: str) -> bool:
    """True when ``line`` is plain paragraph text (joinable)."""
    if not line.strip():
        return False
    if _BLOCK_MARKER.match(line):
        return False
    if _INDENTED_CODE.match(line):
        return False
    return True


def unwrap_soft_breaks(text: str | None) -> str:
    """Join hard-wrapped prose lines back into paragraphs.

    Single newlines between two plain-prose lines are replaced with a
    space; everything else — blank-line paragraph breaks, list items,
    headings, blockquotes, tables, fenced and indented code, thematic
    breaks, and explicit hard breaks (trailing double-space or
    backslash) — is preserved verbatim.

    A prose line is also joined *into* a preceding list-item or
    blockquote line (lazy continuation): the wrapped tail of a long
    bullet belongs to the bullet. It is never joined into a heading,
    table row, fence, or thematic break, where the block ends at the
    newline.

    Idempotent: running the function over its own output is a no-op.
    Returns ``""`` for ``None`` / empty input.
    """
    if not text:
        return ""

    out: list[str] = []
    in_fence = False
    fence_marker = ""

    for line in text.splitlines():
        fence_match = _FENCE.match(line)
        if in_fence:
            out.append(line)
            # The closing fence must use the same character as the
            # opener (``` vs ~~~) and be at least as long.
            if (
                fence_match
                and fence_match.group(1)[0] == fence_marker[0]
                and len(fence_match.group(1)) >= len(fence_marker)
            ):
                in_fence = False
            continue
        if fence_match:
            in_fence = True
            fence_marker = fence_match.group(1)
            out.append(line)
            continue

        if out and _is_prose(line):
            prev = out[-1]
            prev_stripped = prev.strip()
            # Join onto plain prose, list items, and blockquote lines
            # (markdown lazy continuation) — never onto blanks,
            # headings, tables, fences, or thematic breaks, and never
            # past an explicit hard break.
            prev_is_joinable = bool(prev_stripped) and (
                _is_prose(prev) or re.match(r"^\s{0,3}([-*+]\s|\d{1,9}[.)]\s|>)", prev)
            )
            hard_break = prev.endswith("  ") or prev_stripped.endswith("\\")
            if prev_is_joinable and not hard_break:
                out[-1] = f"{prev.rstrip()} {line.strip()}"
                continue

        out.append(line)

    return "\n".join(out)
