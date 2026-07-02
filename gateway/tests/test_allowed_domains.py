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


@pytest.mark.parametrize(
    "domain",
    [
        # Public npm registry for runtime, lockfile-pinned dependency
        # installation (`pnpm install --frozen-lockfile` / `npm ci`) in
        # repos whose dependency sets are too large or dynamic to bake
        # into the sandbox image at build time. Removing this entry
        # silently breaks in-sandbox installs for such repos, so pin its
        # presence here.
        "registry.npmjs.org",
    ],
)
def test_npm_registries_present(domain: str):
    """The npm registry host must stay in the Squid allowlist."""
    text = ALLOWED_DOMAINS_PATH.read_text()
    lines = list(_iter_non_comment_lines(text))
    assert domain in lines, (
        f"{domain!r} missing from allowed_domains.txt. Runtime npm "
        "dependency installation (lockfile-pinned installs through the "
        "Squid proxy) requires this registry host."
    )


@pytest.mark.parametrize(
    "bad_substr",
    [
        # GitHub Packages is deliberately NOT allowlisted: it serves
        # tarballs via a cross-host 302 redirect to a *.githubusercontent.com
        # blob host that would also need allowlisting, and its auth-delivery
        # story into the sandbox is undesigned. Allowlisting the registry
        # host alone yields a confusing failure — metadata resolves 200,
        # then the tarball download is terminated. Don't re-add it without
        # also allowlisting the redirect target(s). Tracked as a follow-up.
        "npm.pkg.github.com",
    ],
)
def test_github_packages_absent(bad_substr: str):
    """GitHub Packages must not appear until its redirect + auth story lands."""
    text = ALLOWED_DOMAINS_PATH.read_text()
    for line in _iter_non_comment_lines(text):
        assert bad_substr not in line.lower(), (
            f"{bad_substr!r} found in allowed_domains.txt on line: {line!r}. "
            "GitHub Packages installs redirect to an un-allowlisted blob "
            "host and would fail at the tarball step; do not re-add it "
            "without allowlisting the redirect target(s) and designing the "
            "auth-delivery path."
        )


@pytest.mark.parametrize(
    "bad_substr",
    [
        # Python installs remain image-bake-only: PyPI must not appear in
        # the runtime egress allowlist.
        "pypi.org",
        "files.pythonhosted.org",
    ],
)
def test_pypi_domains_absent(bad_substr: str):
    """No non-comment line may reference a PyPI host.

    Unlike npm (allowed for runtime lockfile-pinned installs), Python
    dependencies must be pre-installed in the sandbox image.
    """
    text = ALLOWED_DOMAINS_PATH.read_text()
    for line in _iter_non_comment_lines(text):
        assert bad_substr not in line.lower(), (
            f"{bad_substr!r} found in allowed_domains.txt on line: {line!r}. "
            "Python package installation is image-bake-only; PyPI hosts "
            "must not be reachable from the sandbox at runtime."
        )


def test_allowed_domains_has_no_bare_wildcard():
    """A bare ``*`` would defeat the purpose of the allowlist entirely."""
    text = ALLOWED_DOMAINS_PATH.read_text()
    for line in _iter_non_comment_lines(text):
        assert line.strip() != "*", (
            "Bare wildcard '*' in allowed_domains.txt would bypass the Squid "
            "egress policy entirely."
        )
