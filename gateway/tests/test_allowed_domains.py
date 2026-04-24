"""
Sanity tests for ``gateway/allowed_domains.txt``.

Enforces risk-analysis R10 and the refine-phase constraint: all Atlassian
traffic must flow through the gateway's ``/api/v1/jira/*`` endpoints, never
through the Squid egress proxy.  Adding an ``atlassian.*`` entry to the
allowlist would let sandbox containers bypass the gateway's project
allowlist via a direct HTTPS call — this test makes that accidental addition
impossible to land.
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
    ],
)
def test_atlassian_domains_absent(bad_substr: str):
    """No non-comment line may reference an Atlassian domain."""
    text = ALLOWED_DOMAINS_PATH.read_text()
    for line in _iter_non_comment_lines(text):
        assert bad_substr not in line.lower(), (
            f"{bad_substr!r} found in allowed_domains.txt on line: {line!r}. "
            "All Jira traffic must flow through the gateway's /api/v1/jira/* "
            "endpoints, not directly through Squid (issue #1556)."
        )


def test_allowed_domains_has_no_bare_wildcard():
    """A bare ``*`` would defeat the purpose of the allowlist entirely."""
    text = ALLOWED_DOMAINS_PATH.read_text()
    for line in _iter_non_comment_lines(text):
        assert line.strip() != "*", (
            "Bare wildcard '*' in allowed_domains.txt would bypass the Squid "
            "egress policy entirely."
        )
