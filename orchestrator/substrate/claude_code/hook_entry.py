#!/usr/bin/env python3
"""PreToolUse hook entry script for the Claude Code substrate (#2623).

Wired up by ``.claude/settings.json`` (template at
``orchestrator/substrate/claude_code/settings.template.json``):

.. code-block:: json

    {
      "hooks": {
        "PreToolUse": [
          {
            "matcher": "Write|Edit|MultiEdit|NotebookEdit",
            "hooks": [
              {
                "type": "command",
                "command": "python3 -m orchestrator.substrate.claude_code.hook_entry"
              }
            ]
          }
        ]
      }
    }

Claude Code invokes the hook before any matching tool runs. The hook
receives a JSON object on stdin with the tool name and tool input
(see the official PreToolUse hook spec); it returns a JSON object on
stdout. To deny the call, emit::

    {
      "decision": "block",
      "reason": "<human-readable explanation>"
    }

To allow it, emit ``{}`` (or any object without ``decision``).

The allow/deny logic delegates to
``shared/egg_restrictions/checker.py::check_agent_file_access`` —
the **same** symbol the gateway path uses in
``gateway/phase_filter.py:1061 check_agent_restrictions``. There is
no parallel restriction logic in this hook.

The agent role is read from the ``EGG_AGENT_ROLE`` environment
variable (set by the Claude Code substrate's spawner before
launching the agent session). When ``EGG_AGENT_ROLE`` is unset, the
hook allows the call — the substrate has not started enforcing
restrictions, and a fail-open default keeps the user's plain Claude
Code session unaffected by the hook's installation.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _extract_write_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """Return the set of repo-relative paths the tool would write.

    Args:
        tool_name: Name of the Claude Code tool being invoked (e.g.
            ``"Write"``, ``"Edit"``).
        tool_input: The tool's input payload from the hook stdin.

    Returns:
        List of paths the call intends to write. Empty when the tool
        is not a write tool (e.g. ``"Read"``).
    """
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(path, str) and path:
            return [path]
        return []
    if tool_name == "MultiEdit":
        path = tool_input.get("file_path")
        if isinstance(path, str) and path:
            return [path]
        return []
    return []


def _repo_relative(path: str, repo_root: str | None) -> str:
    """Best-effort canonicalisation of ``path`` to a repo-relative key.

    The gateway's ``check_agent_file_access`` matches against repo-
    relative paths. The hook receives absolute paths from Claude
    Code; we strip the repo root prefix when we can resolve one. If
    the file is outside the repo root, return the original path so
    the pattern matcher's blocklist gets a chance to deny it
    structurally (e.g., on ``..`` traversal).
    """
    if not repo_root:
        return path
    try:
        norm_root = os.path.normpath(repo_root)
        norm_path = os.path.normpath(path)
        if norm_path.startswith(norm_root + os.sep):
            return norm_path[len(norm_root) + 1 :]
        if norm_path == norm_root:
            return ""
    except TypeError, ValueError:
        pass
    return path


def decide(stdin_blob: dict[str, Any]) -> dict[str, Any]:
    """Compute the hook decision for a single PreToolUse invocation.

    Args:
        stdin_blob: The parsed JSON object Claude Code wrote to the
            hook's stdin.

    Returns:
        A dict to be JSON-serialized as the hook stdout. Empty or
        missing ``decision`` field means "allow"; ``decision="block"``
        with a ``reason`` blocks the call.
    """
    tool_name = stdin_blob.get("tool_name") or stdin_blob.get("tool", "")
    tool_input = stdin_blob.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    paths = _extract_write_paths(tool_name, tool_input)
    if not paths:
        return {}

    role = os.environ.get("EGG_AGENT_ROLE", "").strip()
    if not role:
        # Hook installed but substrate not currently enforcing a
        # role boundary. Fail-open by design.
        return {}

    repo_root = os.environ.get("EGG_REPO_ROOT") or os.environ.get("EGG_WORKTREE_ROOT")
    repo_paths = [_repo_relative(p, repo_root) for p in paths]

    from egg_restrictions.checker import check_agent_file_access

    allowed, blocked, reason = check_agent_file_access(role, repo_paths, repo=None)
    if allowed:
        return {}

    return {
        "decision": "block",
        "reason": (f"egg PreToolUseHookPolicy denied {tool_name} for role '{role}': {reason}"),
    }


def main() -> int:
    """Read JSON from stdin, write decision JSON to stdout."""
    try:
        raw = sys.stdin.read()
        blob = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        # Failing to parse → fail-open. The alternative (blocking the
        # call) would break the user's session for an unrelated
        # parse error; the gateway path remains the load-bearing
        # enforcement layer.
        print(json.dumps({}))
        return 0

    decision = decide(blob)
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
