"""
Conservative JQL project-scope extractor.

The ``/api/v1/jira/search`` route refuses any JQL it cannot statically prove
is scoped to a set of allowlisted project keys.  This module exposes
``extract_search_projects(jql, allowed)`` which either returns the set of
project keys the query is scoped to, or a specific rejection reason.

Design: parse-then-validate, not regex-search.  We strip comments and string
literals first, then tokenise at top-level boolean operators, then accept
exactly two shapes:

    project = KEY
    project IN (KEY1, KEY2, ...)

…optionally AND-combined with arbitrary additional clauses.  Anything else
— OR at any level, ``project`` compared with a function call, mixed
``key = "FOO-1"`` scope, quoted project keys whose value is not allowlisted,
unicode / mixed-script keys, or a semicolon / comment sneaking through —
returns ``(None, reason)`` and the route translates that to HTTP 403
``jira_search_rejected``.

This is intentionally narrower than Atlassian's JQL grammar.  A more
permissive parser would have to decide whether ``project != NOT_ALLOWED``
"proves" the query only hits allowlisted projects (it doesn't, because it
still matches everything else), and that risk is not worth an extra line of
code.  Agents who hit the ceiling can compose multiple queries.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Project keys follow Atlassian's documented rule:
# uppercase letter followed by letters / digits / underscore.
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TICKET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")

# Characters that must never appear in a sandboxed agent's JQL — they're all
# markers of comment smuggling or statement chaining that the conservative
# parser below would otherwise have to handle specially.
_FORBIDDEN_CHARS: tuple[str, ...] = (";",)
# Characters that split statements / comments in JQL.
_COMMENT_MARKERS: tuple[str, ...] = ("/*", "*/", "--", "//")


class ScopeResult(NamedTuple):
    """Result of a project-scope extraction."""

    projects: frozenset[str] | None  # ``None`` on rejection
    reason: str  # empty on accept, rejection reason otherwise


def extract_search_projects(jql: str, allowed: frozenset[str]) -> ScopeResult:
    """Validate ``jql`` and return the project set it is scoped to.

    Args:
        jql: Raw JQL string received from the sandbox.
        allowed: Project keys the operator has allowlisted.

    Returns:
        ``ScopeResult(projects, "")`` if ``jql`` is statically scoped to a
        subset of ``allowed``; ``ScopeResult(None, reason)`` otherwise.  The
        rejection reason is a short English phrase (``"cannot prove project
        scope"``, ``"project under OR"``, etc.) — routes pass it through
        verbatim into the ``jira_search_rejected`` audit line.
    """
    if not isinstance(jql, str):
        return ScopeResult(None, "jql must be a string")
    if not jql.strip():
        return ScopeResult(None, "jql must not be empty")

    # Non-ASCII is a red flag for unicode homoglyph abuse (e.g. Cyrillic ``А``
    # that looks like Latin ``A``).  Atlassian itself accepts non-ASCII in
    # some fields, but a Jira project key is always ASCII.  Rejecting
    # non-ASCII upfront saves the downstream code from unicode gymnastics.
    try:
        jql.encode("ascii")
    except UnicodeEncodeError:
        return ScopeResult(None, "jql contains non-ASCII characters")

    for forbidden in _FORBIDDEN_CHARS:
        if forbidden in jql:
            return ScopeResult(None, f"jql contains forbidden character '{forbidden}'")
    for marker in _COMMENT_MARKERS:
        if marker in jql:
            return ScopeResult(None, "jql contains comment markers")

    # 1. Strip and normalise string literals, preserving project-key tokens
    #    inside ``IN (...)``.  We replace any single-quoted or double-quoted
    #    substring that contains only project-key characters with the bare
    #    key; anything else becomes the sentinel ``__STR__`` so it can't
    #    contribute to project extraction.
    normalised = _normalise_strings(jql)
    if normalised is None:
        return ScopeResult(None, "jql contains malformed string literal")

    # 2. Split on top-level ``OR`` (case-insensitive) and reject if more than
    #    one disjunct would need to be proven.  Parenthesised ORs are treated
    #    the same — any OR is a rejection unless it's inside an ``IN (...)``
    #    list where we already match the shape strictly.
    if _contains_top_level_or(normalised):
        return ScopeResult(None, "project under OR")

    # 3. Find every ``project`` clause (case-sensitive — we require the
    #    canonical lowercase spelling so agents can't sneak in ``PrOjEcT``
    #    uppercase variants that might bypass a human reviewer's eye).  Also
    #    reject if ``key = ...`` or ``issuekey = ...`` appears on its own,
    #    because those would widen scope without touching ``project``.
    if _contains_bare_key_clause(normalised):
        return ScopeResult(None, "key-level clause without project scope")

    tokens = _extract_project_clauses(normalised)
    if tokens is None:
        return ScopeResult(None, "cannot prove project scope")
    if not tokens:
        return ScopeResult(None, "no project clause")

    # 4. All extracted keys must be in the allowlist.
    not_allowed = [t for t in tokens if t not in allowed]
    if not_allowed:
        return ScopeResult(
            None,
            f"project(s) not allowlisted: {','.join(sorted(set(not_allowed)))}",
        )

    return ScopeResult(frozenset(tokens), "")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _normalise_strings(jql: str) -> str | None:
    """Replace every quoted literal with the sentinel ``__STR__``.

    We deliberately do not preserve the literal value — even when it's a
    valid-looking project key like ``"ENG"``.  The plan-phase adversarial
    suite lists ``project = "ENG"`` (even with an allowlisted key) as a
    rejection, because accepting quoted keys forces the parser to reason
    about string escaping and doesn't buy agents anything they can't get
    from the unquoted spelling.

    Returns ``None`` for malformed literals (mismatched quotes).
    """
    out: list[str] = []
    i = 0
    while i < len(jql):
        ch = jql[i]
        if ch in ('"', "'"):
            # Find the matching close quote.
            end = jql.find(ch, i + 1)
            if end == -1:
                return None
            out.append("__STR__")
            i = end + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _contains_top_level_or(jql: str) -> bool:
    """Return True if the JQL has a top-level OR operator.

    We do a simple scan: split on whitespace, track parenthesis depth, and
    flag an ``OR`` (any case) at depth 0.  This also flags ``OR`` at any
    depth — a stricter rule than "top-level only" — because the
    ``project IN (K, K, K)`` shape never contains an ``OR`` token anyway, so
    any ``OR`` at all is a rejection.  Belt-and-braces.
    """
    # Tokenise preserving punctuation.  We specifically care about the literal
    # token ``OR`` (not the word boundary in names), so we match it as a
    # whole-word case-insensitive pattern.
    return re.search(r"(?i)(?<![A-Za-z0-9_])or(?![A-Za-z0-9_])", jql) is not None


def _contains_bare_key_clause(jql: str) -> bool:
    """Return True if the JQL references ``key`` / ``issuekey`` / ``id`` as a
    filter clause.  These widen scope without anchoring on ``project``.
    """
    pattern = re.compile(
        r"(?i)(?<![A-Za-z0-9_])(key|issuekey|id)\s*(=|!=|in|not\s+in|~|>|<)",
    )
    return pattern.search(jql) is not None


def _extract_project_clauses(jql: str) -> list[str] | None:
    """Pull the project keys out of every ``project`` clause in ``jql``.

    Only accepts the exact shapes:

        project = KEY              (case-sensitive 'project', uppercase KEY)
        project IN (KEY[, KEY]...) (case-sensitive 'project')

    Returns the flat list of keys on success, an empty list if no clause is
    present, or ``None`` on any malformed / case-variant occurrence.
    """
    out: list[str] = []
    # We need both case-sensitive and case-insensitive scans: the former to
    # find the canonical ``project = KEY`` shape, the latter to detect any
    # non-canonical spelling (``PROJECT = KEY``, ``Project = KEY``) so we can
    # reject it.
    all_matches = list(re.finditer(r"(?i)(?<![A-Za-z0-9_])project(?![A-Za-z0-9_])", jql))
    canonical_matches = list(re.finditer(r"(?<![A-Za-z0-9_])project(?![A-Za-z0-9_])", jql))
    if len(all_matches) != len(canonical_matches):
        # Some ``project`` reference isn't the canonical lowercase spelling.
        return None

    # Build a precise regex for the accepted shapes.
    # Shape A: ``project = KEY``
    shape_a = re.compile(
        r"(?<![A-Za-z0-9_])project\s*=\s*([A-Z][A-Z0-9_]*)(?![A-Za-z0-9_])",
    )
    # Shape B: ``project IN (KEY[, KEY]...)``  (case-sensitive ``project``,
    # case-insensitive ``IN``)
    shape_b = re.compile(
        r"(?<![A-Za-z0-9_])project\s+(?i:in)\s*\(\s*([A-Z][A-Z0-9_]*(?:\s*,\s*[A-Z][A-Z0-9_]*)*)\s*\)",
    )
    accepted_spans: list[tuple[int, int]] = []
    for m in shape_a.finditer(jql):
        out.append(m.group(1))
        accepted_spans.append(m.span())
    for m in shape_b.finditer(jql):
        for key in re.split(r"\s*,\s*", m.group(1)):
            out.append(key)
        accepted_spans.append(m.span())

    # Every canonical ``project`` token must have been consumed by one of the
    # accepted shapes.  If any is left over, the query has an unsupported
    # ``project`` construct (``project = projectsLeadByUser()``,
    # ``project != FOO``, ``project ~ "text"``, etc.).
    for m in canonical_matches:
        if not any(start <= m.start() < end for start, end in accepted_spans):
            return None

    # Sanity: every extracted key must still look like a project key.
    for key in out:
        if not _PROJECT_KEY_RE.fullmatch(key):
            return None

    return out


__all__ = [
    "ScopeResult",
    "extract_search_projects",
]
