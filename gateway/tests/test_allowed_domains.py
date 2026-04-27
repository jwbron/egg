"""
Sanity tests for ``gateway/allowed_domains.txt``.

Enforces risk-analysis R10 and the refine-phase constraint: all Atlassian
traffic must flow through the gateway's ``/api/v1/jira/*`` and (since
#1931) ``/api/v1/confluence/*`` endpoints, never through the Squid egress
proxy.  Adding an ``atlassian.*`` entry to the allowlist would let sandbox
containers bypass the gateway's project / space allowlist via a direct
HTTPS call — this test makes that accidental addition impossible to land.

The Confluence wrapper added in #1931 reuses the same Atlassian Cloud
hostnames, so the existing ``*.atlassian.*`` block-list invariant covers
Confluence by extension; the Confluence-specific entries below are
defensive — even if Atlassian ever exposes a separate hostname like
``wiki.atlassian.net`` or ``confluence.atlassian.com``, the allowlist must
still reject it.  ``grep -i confluence gateway/tests/`` should find this
file so the invariant is discoverable from the Confluence side too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ALLOWED_DOMAINS_PATH = Path(__file__).parent.parent / "allowed_domains.txt"


def _iter_non_comment_lines(text: str):
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        yield line


def test_allowed_domains_file_exists():
    assert ALLOWED_DOMAINS_PATH.exists(), (
        "gateway/allowed_domains.txt is missing — Squid won't come up."
    )


@pytest.mark.parametrize(
    "bad_substr",
    [
        "atlassian.net",
        "atlassian.com",
        "api.atlassian.com",
        "jira.atlassian.com",
        # Confluence-specific hostnames (#1931).  Defence-in-depth even
        # though Atlassian Cloud uses ``<tenant>.atlassian.net/wiki/...``
        # — if Atlassian ever exposes a Confluence-only hostname the
        # allowlist must still reject it.
        "wiki.atlassian.net",
        "confluence.atlassian.com",
    ],
)
def test_atlassian_domains_absent(bad_substr: str):
    """No non-comment line may reference an Atlassian (Jira / Confluence) domain.

    All Atlassian traffic — Jira read endpoints (#1556) and Confluence
    read endpoints (#1931) — must flow through the gateway's
    ``/api/v1/jira/*`` / ``/api/v1/confluence/*`` routes, not directly
    through Squid.
    """
    text = ALLOWED_DOMAINS_PATH.read_text()
    for line in _iter_non_comment_lines(text):
        assert bad_substr not in line.lower(), (
            f"{bad_substr!r} found in allowed_domains.txt on line: {line!r}. "
            "All Atlassian traffic must flow through the gateway's "
            "/api/v1/jira/* and /api/v1/confluence/* endpoints, not "
            "directly through Squid (issues #1556, #1931)."
        )


def test_allowed_domains_has_no_bare_wildcard():
    """A bare ``*`` would defeat the purpose of the allowlist entirely."""
    text = ALLOWED_DOMAINS_PATH.read_text()
    for line in _iter_non_comment_lines(text):
        assert line.strip() != "*", (
            "Bare wildcard '*' in allowed_domains.txt would bypass the Squid "
            "egress policy entirely."
        )
