"""
Atlassian Document Format (ADF) helpers for Jira write verbs.

Atlassian Cloud's REST API v3 expects rich-text fields (``description``,
``comment.body``) to be serialised as ADF — a JSON document tree, not a plain
string or wiki markup.  Agents typically only have plain text to send, so the
gateway transparently wraps it in the minimal ADF document shape.  Callers
that already speak ADF (e.g. an orchestrator that built a structured comment
with bullet lists) can pass a pre-built dict and we will leave it alone.

Implements decision-7 (accept both plain text and ADF dicts) from the
architect analysis for issue [#1924](https://github.com/jwbron/egg/issues/1924).

This module is intentionally pure-Python — no httpx, no Flask, no Atlassian
credentials.  It is unit-testable in isolation and imported by both
``gateway/jira_client.py`` (the write methods) and ``gateway/gateway.py`` (the
route-layer body validators).

ADF reference: https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/
"""

from __future__ import annotations

from typing import Any


def wrap_text_as_adf(text: str) -> dict[str, Any]:
    """Return the minimal ADF document that renders ``text`` as a single paragraph.

    Atlassian rejects ADF documents whose ``content`` array is empty (the
    paragraph node must contain at least one child), so the empty-string case
    is wrapped as an empty paragraph with no text node.  This matches the
    shape Atlassian sends back when a user posts an empty comment via the
    web UI.

    Newlines in the input are preserved as-is inside a single text node;
    ADF treats text nodes opaquely.  Callers that want hard line breaks
    (separate paragraphs, ``hardBreak`` nodes) should pre-build the ADF dict
    themselves and pass it through ``is_adf_dict``.

    Args:
        text: Plain-text body to wrap.  ``None`` is rejected (callers should
            pass an empty string for "no description").

    Returns:
        A new dict each call (callers may mutate it without contaminating
        future calls).
    """
    if text is None:
        raise TypeError("wrap_text_as_adf requires a string, got None")
    if not isinstance(text, str):
        raise TypeError(f"wrap_text_as_adf requires a string, got {type(text).__name__}")

    if text == "":
        # Empty paragraph — Atlassian renders this as a blank line, matching
        # what the web UI emits for an empty body.
        return {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": []}],
        }

    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def is_adf_dict(value: Any) -> bool:
    """Return True iff ``value`` looks like a structurally-valid ADF document.

    Catches the common cases — top-level dict, ``type=="doc"``, integer
    ``version``, list-shaped ``content`` — without doing a full schema walk
    (Atlassian validates the rest server-side).  Callers use this to decide
    between "wrap this plain text" and "pass this dict through unchanged".

    Specifically rejects:

    - Non-dicts (strings, lists, numbers, ``None``).
    - Dicts missing ``type`` or with ``type != "doc"``.
    - Dicts whose ``version`` is missing or not an integer (booleans count
      as ints in Python; we reject them explicitly because ``True``/``False``
      are never valid ADF version values).
    - Dicts whose ``content`` is missing or not a list.

    Args:
        value: Any candidate value from a request body.

    Returns:
        ``True`` if the value's outer envelope matches ADF; ``False`` otherwise.
    """
    if not isinstance(value, dict):
        return False
    if value.get("type") != "doc":
        return False
    version = value.get("version")
    # Reject booleans even though ``bool`` is a subclass of ``int`` in Python —
    # ``{"version": True}`` is structurally suspicious and should not be
    # treated as a valid ADF doc.
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    if not isinstance(value.get("content"), list):
        return False
    return True


__all__ = [
    "is_adf_dict",
    "wrap_text_as_adf",
]
