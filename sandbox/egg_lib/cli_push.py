"""Push subcommand for egg-orch CLI.

Thin wrapper around ``git push`` — nothing more.  The ``--scope-filter``
flag, its ``_filter_files`` helper, and the ``EGG_AGENT_FILE_PATTERNS``
env var were removed in #1882 when the gateway started auto-filtering
disallowed files during ``/api/v1/git/push``.  Agents that used to run
``egg-orch push --scope-filter`` to recover from a 403 now just run
``egg-orch push``; the gateway rewrites the range per-commit and
surfaces the excluded files in the response.
"""

from __future__ import annotations

import argparse
import os
import subprocess as subprocess
import sys


def _get_current_branch() -> str:
    """Return the current branch name."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "").strip()


def _retarget_refspec(current_branch: str) -> str | None:
    """Return ``HEAD:<assigned>`` when the push must target the pipeline's assigned branch.

    Pipeline agents run on per-agent work branches (``egg/<pid>-<role>/work``)
    but the gateway locks the session to the pipeline's assigned branch
    (``egg/<pid>``).  When ``EGG_BRANCH`` is set and differs from
    ``current_branch`` the push must use ``HEAD:<assigned>`` so the
    refspec matches the gateway's push-target check.

    Returns ``None`` when no retargeting is needed.
    """
    assigned = os.environ.get("EGG_BRANCH", "").strip()
    if assigned and assigned != current_branch:
        return f"HEAD:{assigned}"
    return None


def cmd_push(args: argparse.Namespace) -> None:
    """Handle the push subcommand — pass through to git push."""
    refspec = _retarget_refspec(_get_current_branch())
    if refspec:
        result = subprocess.run(["git", "push", "origin", refspec], text=True, check=False)
    else:
        result = subprocess.run(["git", "push"], text=True, check=False)
    sys.exit(result.returncode)


def register_push_subcommand(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the ``push`` subcommand on the given subparsers."""
    push_parser = subparsers.add_parser(
        "push",
        help="Push the current branch (gateway auto-filters out-of-scope files).",
    )
    push_parser.set_defaults(func=cmd_push)
