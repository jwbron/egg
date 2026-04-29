"""Shared path validation for filesystem tools (defense-in-depth).

Provides :func:`validate_file_path` which resolves symlinks and checks
that the resulting path is within an allowed directory.  This supplements
Docker container isolation with tool-level enforcement.
"""

from __future__ import annotations

import os
from pathlib import Path

# Default allowed root directories.  The agent workspace is always
# allowed; /tmp is allowed for scratch files.
_DEFAULT_ALLOWED_ROOTS: tuple[str, ...] = (
    os.path.expanduser("~/repos"),
    "/tmp",
)


def validate_file_path(
    file_path: str,
    *,
    allowed_roots: tuple[str, ...] | None = None,
) -> str | None:
    """Validate that *file_path* is within an allowed directory.

    Resolves symlinks via :meth:`Path.resolve()` to prevent traversal
    attacks (e.g. ``/home/egg/repos/../../etc/shadow``).

    Args:
        file_path: The path to validate.
        allowed_roots: Tuple of allowed root directories.  Defaults to
            ``~/repos`` and ``/tmp``.

    Returns:
        An error message if the path is outside allowed boundaries, or
        ``None`` if the path is valid.
    """
    if allowed_roots is None:
        allowed_roots = _DEFAULT_ALLOWED_ROOTS

    # Also allow EGG_REPO_PATH if set.
    repo_path = os.environ.get("EGG_REPO_PATH")
    if repo_path:
        allowed_roots = (*allowed_roots, repo_path)

    try:
        resolved = Path(file_path).resolve()
    except OSError, ValueError:
        return f"Invalid path: {file_path}"

    resolved_str = str(resolved)
    for root in allowed_roots:
        try:
            root_resolved = str(Path(root).resolve())
        except OSError, ValueError:
            continue
        if resolved_str == root_resolved or resolved_str.startswith(root_resolved + os.sep):
            return None

    return (
        f"Path {file_path!r} is outside the allowed workspace. "
        f"File operations are restricted to: {', '.join(allowed_roots)}"
    )
