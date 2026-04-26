"""Canonical infra-error pattern list (issue #1962).

The overseer should NOT recommend filing a GitHub issue (and should not
escalate to the human as a bug) when the underlying anomaly was caused by
infrastructure transients — gh API rate limits, container OOM, network
DNS failures, etc. Those are operational events, not engineering
defects, and surfacing them as issues creates noise.

This module is the single source of truth for the patterns. Both the
sandbox-side overseer monitor and the orchestrator-side dead
``issue_filer.py`` reference it so a single edit covers both call sites.
"""

from __future__ import annotations

import re

# Each entry is (compiled-regex, kind). Kind is a short stable string
# the caller can include in a structured log line.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # GitHub API rate limiting.
    (re.compile(r"API rate limit exceeded", re.IGNORECASE), "gh-rate-limit"),
    (re.compile(r"secondary rate limit", re.IGNORECASE), "gh-secondary-rate-limit"),
    (re.compile(r"abuse detection", re.IGNORECASE), "gh-abuse-detection"),
    # Container resource pressure.
    (re.compile(r"OOMKilled", re.IGNORECASE), "container-oom"),
    (re.compile(r"out of memory", re.IGNORECASE), "container-oom"),
    (re.compile(r"cannot allocate memory", re.IGNORECASE), "container-oom"),
    # Network / DNS failures.
    (re.compile(r"Temporary failure in name resolution"), "network-dns"),
    (re.compile(r"could not resolve host", re.IGNORECASE), "network-dns"),
    (re.compile(r"connection refused", re.IGNORECASE), "network-conn"),
    (re.compile(r"connection reset by peer", re.IGNORECASE), "network-conn"),
    (re.compile(r"i/o timeout", re.IGNORECASE), "network-timeout"),
]


def is_infra_error(text: str) -> bool:
    """Return True if ``text`` matches any known infra-transient pattern.

    Args:
        text: Free-form text to scan (e.g. a log line, a classifier
            ``reasoning`` string, or an error message).

    Returns:
        True if any pattern matches; False otherwise. Empty / None-ish
        input is treated as not matching.
    """
    if not text:
        return False
    for compiled, _ in _PATTERNS:
        if compiled.search(text):
            return True
    return False


def classify_infra_error(text: str) -> str | None:
    """Return the ``kind`` for the first matching pattern, or None.

    Args:
        text: Free-form text to scan.

    Returns:
        The matched pattern's stable kind string (e.g. ``"gh-rate-limit"``)
        or ``None`` if no match.
    """
    if not text:
        return None
    for compiled, kind in _PATTERNS:
        if compiled.search(text):
            return kind
    return None


__all__ = ["is_infra_error", "classify_infra_error"]
