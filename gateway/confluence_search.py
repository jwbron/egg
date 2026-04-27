"""
Conservative CQL space-scope extractor.

The ``/api/v1/confluence/search`` route refuses any CQL it cannot statically
prove is scoped to a set of allowlisted space keys.  This module exposes
``extract_search_spaces(cql, allowed)`` which either returns the set of
space keys the query is scoped to, or a specific rejection reason.

Design: parse-then-validate, not regex-search.  We strip comments and
string literals first, then tokenise at top-level boolean operators, then
accept exactly two shapes::

    space = KEY
    space IN (KEY1, KEY2, ...)

…optionally AND-combined with arbitrary additional clauses.  Anything else
— ``OR`` at any level, ``space`` compared with a function call, mixed
``id = ...`` scope, quoted space keys whose value is not allowlisted,
unicode / mixed-script keys, or a semicolon / comment sneaking through —
returns ``(None, reason)`` and the route translates that to HTTP 403
``confluence_search_rejected``.

This is intentionally narrower than Atlassian's CQL grammar.  A more
permissive parser would have to decide whether ``space != NOT_ALLOWED``
"proves" the query only hits allowlisted spaces (it doesn't, because it
still matches everything else), and that risk is not worth an extra line
of code.  Agents who hit the ceiling can compose multiple queries.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Atlassian space keys are conventionally uppercase but the API accepts mixed
# case.  Anchor on a leading letter and accept letters/digits/underscore.
_SPACE_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

# Characters that must never appear in a sandboxed agent's CQL — they're all
# markers of comment smuggling or statement chaining the conservative parser
# below would otherwise have to handle specially.
_FORBIDDEN_CHARS: tuple[str, ...] = (";",)
# Characters that split statements / comments in CQL.
_COMMENT_MARKERS: tuple[str, ...] = ("/*", "*/", "--", "//")


class ScopeResult(NamedTuple):
    """Result of a space-scope extraction."""

    spaces: frozenset[str] | None  # ``None`` on rejection
    reason: str  # empty on accept, rejection reason otherwise


def extract_search_spaces(cql: str, allowed: frozenset[str]) -> ScopeResult:
    """Validate ``cql`` and return the space set it is scoped to.

    Args:
        cql: Raw CQL string received from the sandbox.
        allowed: Space keys the operator has allowlisted.

    Returns:
        ``ScopeResult(spaces, "")`` if ``cql`` is statically scoped to a
        subset of ``allowed``; ``ScopeResult(None, reason)`` otherwise.  The
        rejection reason is a short English phrase passed through verbatim
        into the ``confluence_search_rejected`` audit line.
    """
    if not isinstance(cql, str):
        return ScopeResult(None, "cql must be a string")
    if not cql.strip():
        return ScopeResult(None, "cql must not be empty")

    # Non-ASCII is a red flag for unicode homoglyph abuse.
    try:
        cql.encode("ascii")
    except UnicodeEncodeError:
        return ScopeResult(None, "cql contains non-ASCII characters")

    for forbidden in _FORBIDDEN_CHARS:
        if forbidden in cql:
            return ScopeResult(None, f"cql contains forbidden character '{forbidden}'")
    for marker in _COMMENT_MARKERS:
        if marker in cql:
            return ScopeResult(None, "cql contains comment markers")

    # 1. Replace every quoted literal with the sentinel ``__STR__`` so it
    #    can't masquerade as a space key.  Even ``space = "ENG"`` (with an
    #    allowlisted key) is rejected — accepting quoted keys forces the
    #    parser to reason about string escaping and doesn't buy agents
    #    anything they can't get from the unquoted spelling.
    normalised = _normalise_strings(cql)
    if normalised is None:
        return ScopeResult(None, "cql contains malformed string literal")

    # 2. Reject any OR (case-insensitive) at any depth.  ``space IN (K, K)``
    #    never contains an OR token, so any OR is a rejection.
    if _contains_or(normalised):
        return ScopeResult(None, "space under OR")

    # 3. Reject id / content / title clauses entirely (regardless of any
    #    accompanying space anchor).  Each one widens scope past what the
    #    space-only extractor below can prove.
    if _contains_id_clause(normalised):
        return ScopeResult(
            None,
            "id, content, and title clauses are not supported; use 'text ~ ...' instead",
        )

    tokens = _extract_space_clauses(normalised)
    if tokens is None:
        return ScopeResult(None, "cannot prove space scope")
    if not tokens:
        return ScopeResult(None, "no space clause")

    # 4. All extracted keys must be in the allowlist.
    not_allowed = [t for t in tokens if t not in allowed]
    if not_allowed:
        return ScopeResult(
            None,
            f"space(s) not allowlisted: {','.join(sorted(set(not_allowed)))}",
        )

    return ScopeResult(frozenset(tokens), "")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _normalise_strings(cql: str) -> str | None:
    """Replace every quoted literal with the sentinel ``__STR__``.

    Returns ``None`` for malformed literals (mismatched quotes).
    """
    out: list[str] = []
    i = 0
    while i < len(cql):
        ch = cql[i]
        if ch in ('"', "'"):
            end = cql.find(ch, i + 1)
            if end == -1:
                return None
            out.append("__STR__")
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _contains_or(cql: str) -> bool:
    """Return True if the CQL contains an ``OR`` boolean operator at any depth."""
    return re.search(r"(?i)(?<![A-Za-z0-9_])or(?![A-Za-z0-9_])", cql) is not None


def _contains_id_clause(cql: str) -> bool:
    """Return True if the CQL references ``id`` / ``content`` / ``title``.

    These clauses are rejected unconditionally — including when an
    accompanying ``space`` anchor is present — because the static extractor
    only proves space scope for the exact ``space = K`` / ``space IN (...)``
    shapes and cannot reason about how an ``id`` / ``content`` / ``title``
    filter widens (or fails to widen) the result set.
    """
    pattern = re.compile(
        r"(?i)(?<![A-Za-z0-9_])(id|content|title)\s*(=|!=|in|not\s+in|~|>|<)",
    )
    return pattern.search(cql) is not None


def _extract_space_clauses(cql: str) -> list[str] | None:
    """Pull the space keys out of every ``space`` clause in ``cql``.

    Only accepts the exact shapes::

        space = KEY              (case-sensitive 'space', valid space key)
        space IN (KEY[, KEY]...) (case-sensitive 'space')

    Returns the flat list of keys on success, an empty list if no clause is
    present, or ``None`` on any malformed / case-variant occurrence.
    """
    out: list[str] = []
    # Detect any non-canonical capitalisation (``Space``, ``SPACE``).
    all_matches = list(re.finditer(r"(?i)(?<![A-Za-z0-9_])space(?![A-Za-z0-9_])", cql))
    canonical_matches = list(re.finditer(r"(?<![A-Za-z0-9_])space(?![A-Za-z0-9_])", cql))
    if len(all_matches) != len(canonical_matches):
        return None

    # Precise regex for the accepted shapes.
    shape_a = re.compile(
        r"(?<![A-Za-z0-9_])space\s*=\s*([a-zA-Z][a-zA-Z0-9_]*)(?![A-Za-z0-9_])",
    )
    shape_b = re.compile(
        r"(?<![A-Za-z0-9_])space\s+(?i:in)\s*\(\s*"
        r"([a-zA-Z][a-zA-Z0-9_]*(?:\s*,\s*[a-zA-Z][a-zA-Z0-9_]*)*)\s*\)",
    )
    accepted_spans: list[tuple[int, int]] = []
    for m in shape_a.finditer(cql):
        out.append(m.group(1))
        accepted_spans.append(m.span())
    for m in shape_b.finditer(cql):
        for key in re.split(r"\s*,\s*", m.group(1)):
            out.append(key)
        accepted_spans.append(m.span())

    # Every canonical ``space`` token must have been consumed by one of the
    # accepted shapes.  Otherwise ``space != FOO`` etc. would slip through.
    for m in canonical_matches:
        if not any(start <= m.start() < end for start, end in accepted_spans):
            return None

    for key in out:
        if not _SPACE_KEY_RE.fullmatch(key):
            return None

    return out


__all__ = [
    "ScopeResult",
    "extract_search_spaces",
]
