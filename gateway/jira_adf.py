"""
Atlassian Document Format (ADF) helpers for Jira write verbs.

Atlassian's REST API v3 expects rich-text fields (``description``, comment
``body``) as ADF — a JSON tree of typed nodes.  The full ADF spec is large
but Atlassian accepts a minimal "plain paragraph" shape that is sufficient
for everything the egg gateway needs to write today.

Public surface:

- ``wrap_text_as_adf(text)`` — produce a minimal ADF doc whose body is the
  given plain-text string broken on ``\\n`` into paragraphs.  Empty input
  yields an empty paragraph (still a valid ADF doc).
- ``is_adf_dict(value)`` — structural test for "looks like an ADF doc".
  Used by callers that accept either a plain-text string or a pre-built
  ADF dict and need to decide whether to wrap.

Notes:

- These helpers do **not** sanitise / validate the contents of pre-built
  ADF dicts beyond the structural test in ``is_adf_dict``.  Body content
  always crosses to Atlassian; the gateway's role is to wrap-or-passthrough
  and to enforce its own size caps in the route layer.
- The minimal shape mirrors the example Atlassian publishes in the v3
  REST API reference and what the official ADF builder library produces
  for ``Doc().paragraph(Text(s))``.
"""

from __future__ import annotations

from typing import Any

# ADF document version expected by Atlassian's v3 REST API (and the only
# one we emit).  Atlassian has shipped v1 since 2018.
_ADF_VERSION: int = 1


def wrap_text_as_adf(text: str) -> dict[str, Any]:
    """Return a minimal ADF doc wrapping ``text`` as paragraph(s).

    Newlines split the text into separate ``paragraph`` nodes; this matches
    how Atlassian renders pasted plain text in the web UI.  An empty / blank
    string produces an empty paragraph (Atlassian still treats that as a
    valid doc).

    Args:
        text: Plain-text content.  ``None`` is normalised to ``""``.

    Returns:
        A dict suitable for use as the ``description`` field of a
        ``createIssue`` body, the ``body`` field of an ``addComment``
        body, etc.
    """
    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)

    if text == "":
        return {
            "type": "doc",
            "version": _ADF_VERSION,
            "content": [
                {"type": "paragraph", "content": []},
            ],
        }

    paragraphs: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if line == "":
            # Empty line between paragraphs — emit an empty paragraph so
            # Atlassian preserves the blank line in rendering.
            paragraphs.append({"type": "paragraph", "content": []})
        else:
            paragraphs.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line}],
                }
            )

    return {
        "type": "doc",
        "version": _ADF_VERSION,
        "content": paragraphs,
    }


def is_adf_dict(value: object) -> bool:
    """Return True iff ``value`` looks like an ADF document.

    The structural shape we accept:

    * is a dict
    * has ``type == "doc"``
    * has a ``version`` int
    * has a ``content`` list

    We don't recurse into ``content`` — Atlassian itself validates the deep
    structure server-side, and the gateway has no business reimplementing
    the ADF schema.
    """
    if not isinstance(value, dict):
        return False
    if value.get("type") != "doc":
        return False
    if not isinstance(value.get("version"), int):
        return False
    if not isinstance(value.get("content"), list):
        return False
    return True


__all__ = [
    "is_adf_dict",
    "wrap_text_as_adf",
]
