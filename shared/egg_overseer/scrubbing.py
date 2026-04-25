"""Secret-scrubbing helper for overseer-filed issue bodies (R-SEC-01).

Issue #1962 ships an auto-issue-filing path where the advisor (Opus 4.6)
composes an issue body from container logs + classification context. The
body could carry leaked secrets (GitHub PATs, AWS keys, Slack webhooks,
``GITHUB_TOKEN=...`` exports). This module replaces matches with a
``[REDACTED:<kind>]`` marker so the rendered body never carries the raw
secret to the public GitHub issue.

The advisor is the **primary** scrubber: it must call ``scrub_secrets``
before returning its verdict. The sandbox-side body composer
(``sandbox/egg_lib/overseer_issue_body.py``) calls it again as
defense-in-depth. The gateway scans for the same patterns one more time
and rejects any body that still contains a match — that way an advisor
bug doesn't ship a secret to GitHub.

Pattern coverage:

* GitHub PATs / tokens: ``ghp_`` / ``ghs_`` / ``gho_`` / ``ghu_`` /
  ``ghr_`` followed by 36 base62 characters.
* AWS access keys: ``AKIA`` followed by 16 uppercase alphanumerics.
* Slack incoming-webhook URLs: ``https://hooks.slack.com/services/...``.
* Generic env exports: ``GITHUB_TOKEN``, ``GH_TOKEN``,
  ``ANTHROPIC_API_KEY`` followed by ``=value``.

The function is deterministic, idempotent (one pass replaces all known
patterns; running it twice on the output yields the same string), and
``O(N)`` in body length. Bodies are bounded at 50 KB by gateway policy
so this is microsecond-scale.
"""

from __future__ import annotations

import re

# Each entry: (compiled-regex, redaction-marker).
# Order matters only for readability — the patterns do not overlap.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
        "[REDACTED:gh-pat]",
    ),
    (
        re.compile(r"\bghs_[A-Za-z0-9]{36}\b"),
        "[REDACTED:gh-pat]",
    ),
    (
        re.compile(r"\bgho_[A-Za-z0-9]{36}\b"),
        "[REDACTED:gh-pat]",
    ),
    (
        re.compile(r"\bghu_[A-Za-z0-9]{36}\b"),
        "[REDACTED:gh-pat]",
    ),
    (
        re.compile(r"\bghr_[A-Za-z0-9]{36}\b"),
        "[REDACTED:gh-pat]",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED:aws-key]",
    ),
    (
        re.compile(r"https://hooks\.slack\.com/services/\S+"),
        "[REDACTED:slack-webhook]",
    ),
    (
        re.compile(r"\b(?:GITHUB_TOKEN|GH_TOKEN|ANTHROPIC_API_KEY)\s*=\s*\S+"),
        "[REDACTED:env-export]",
    ),
]


# A separate predicate-only list mirrors the same patterns so the
# gateway can reject a body without also mutating it. Keeping the
# pattern source colocated avoids drift between the scrubber and the
# defense-in-depth gateway check.
SECRET_PATTERN_KINDS: list[tuple[re.Pattern[str], str]] = [
    (compiled, marker.removeprefix("[REDACTED:").rstrip("]")) for compiled, marker in _PATTERNS
]


def scrub_secrets(text: str) -> str:
    """Replace known-secret patterns with ``[REDACTED:<kind>]`` markers.

    Args:
        text: Free-form body text that may contain leaked secrets.

    Returns:
        ``text`` with every match replaced by a redaction marker. Calling
        this function on a string that has no matches returns the input
        byte-for-byte. Calling it twice in a row is idempotent because
        the redaction markers themselves do not match any pattern.
    """
    if not text:
        return text
    out = text
    for compiled, marker in _PATTERNS:
        out = compiled.sub(marker, out)
    return out


def find_secret_kinds(text: str) -> list[str]:
    """Return the set of secret-pattern kinds present in ``text``.

    Used by the gateway as a defense-in-depth check: if the advisor's
    scrubber missed something, the gateway rejects the body and reports
    the discovered kinds in a structured error.

    Args:
        text: Body text to scan.

    Returns:
        Sorted list of unique kinds (e.g.
        ``["aws-key", "gh-pat"]``). Empty list if no matches.
    """
    if not text:
        return []
    kinds: set[str] = set()
    for compiled, kind in SECRET_PATTERN_KINDS:
        if compiled.search(text):
            kinds.add(kind)
    return sorted(kinds)


__all__ = [
    "SECRET_PATTERN_KINDS",
    "scrub_secrets",
    "find_secret_kinds",
]
