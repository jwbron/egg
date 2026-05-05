"""
Canonical glob-pattern matcher for agent / phase / role file boundaries.

This is the single ``match_pattern`` implementation shared by every layer
that decides whether a path matches a configured pattern:

- ``AgentFilePattern.can_write`` — per-role attribution (this package)
- ``FileRestriction.is_file_blocked`` — gateway early-reject
  (``gateway/phase_filter.py``)
- ``PhaseFileRestriction.is_file_allowed`` — phase-level allow/block
  (``gateway/phase_filter.py``)
- ``FileAccessPattern.can_read`` / ``can_write`` — orchestrator
  role-definition surface (``shared/egg_contracts/agent_roles.py``)

#1903 unified the first two layers; #2356 collapses the remaining
two onto this module so all four go through the same matcher and a
``**/<dir>/`` pattern in any of those configurations means the same
thing everywhere.

The function lives in its own module so consumers in ``egg_contracts``
can import it without re-introducing the ``egg_restrictions →
egg_contracts → egg_restrictions`` import cycle that ``patterns.py``'s
``AgentRole`` re-export established.
"""

from __future__ import annotations

import fnmatch

__all__ = ["match_pattern"]


def match_pattern(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a glob-like pattern.

    Supports:
    - Exact match: "foo/bar.py"
    - Prefix match: "foo/" (matches any file under foo/)
    - Wildcard: "*.py" (matches files ending in .py)
    - Double wildcard: "**/*.py" (matches .py files at any depth)
    - Directory at any depth: "**/tests/" (matches any file under a
      ``tests/`` directory at any depth, including top-level)

    Args:
        file_path: Path to check
        pattern: Pattern to match against

    Returns:
        True if the path matches the pattern
    """
    # Normalize both paths
    file_path = file_path.lstrip("./")
    pattern = pattern.lstrip("./")

    # Directory patterns containing ** (e.g., "**/tests/", "**/test/")
    # must be handled BEFORE the bare-prefix branch because the bare-prefix
    # branch uses `file_path.startswith(pattern)` which would miss nested
    # files like "gateway/tests/__init__.py" against "**/tests/".
    # Fix for #1901 — previously the ** branch's fnmatch-on-basename logic
    # returned False for nested directory files.
    if pattern.endswith("/") and "**" in pattern:
        # A pattern like "**/<dir>/" matches any file under a directory of
        # that name at any depth, including top-level (zero segments before).
        # We strip the leading "**/" and the trailing "/" to extract the
        # directory segment(s) to look for.
        inner = pattern
        if inner.startswith("**/"):
            inner = inner[3:]
        # Strip a single leading "**" if someone wrote "**<dir>/" (unusual)
        elif inner.startswith("**"):
            inner = inner[2:]
        # inner is now e.g. "tests/" — split it into a path prefix we
        # look for as a complete segment inside file_path.
        dir_segment = inner.rstrip("/")
        if not dir_segment:
            return False
        # Match only if dir_segment appears as a complete path segment
        # (i.e. surrounded by / or at path start) AND there is at least
        # one more path segment after it (it must be a directory, not a
        # leaf filename).
        parts = file_path.split("/")
        # dir_segment may itself contain slashes (e.g. "a/b"); handle both.
        seg_parts = dir_segment.split("/")
        seg_len = len(seg_parts)
        if seg_len == 0:
            return False
        # Scan each possible starting index.
        for i in range(0, len(parts) - seg_len):
            if parts[i : i + seg_len] == seg_parts:
                return True
        return False

    # Prefix match (directory pattern)
    if pattern.endswith("/"):
        return file_path.startswith(pattern) or file_path + "/" == pattern

    # Handle ** patterns for recursive matching
    if "**" in pattern:
        # Convert ** to regex-style matching
        # "**/*.py" should match "foo/bar/baz.py"
        parts = pattern.split("**")
        if len(parts) == 2:
            prefix, suffix = parts
            # Check if path starts with prefix (if any) and ends with suffix
            prefix_match = not prefix or file_path.startswith(prefix.rstrip("/"))
            suffix = suffix.lstrip("/")
            suffix_match = not suffix or fnmatch.fnmatch(file_path.split("/")[-1], suffix)
            if prefix_match and suffix_match:
                # For patterns like "**/*.py", also check the full suffix matches
                if suffix.startswith("*"):
                    return fnmatch.fnmatch(file_path, "*" + suffix)
                return True

    # Standard fnmatch for simple wildcards
    return fnmatch.fnmatch(file_path, pattern)
