"""Shared BRC consensus-push helper.

This module exposes :func:`consensus_push` — the single implementation
of "push code to the gateway with the ``consensus_push`` marker" shared
between the ``egg-orch consensus propose --push`` CLI shim and the
``mcp__brc__propose`` MCP tool wrapper.  Both surfaces MUST route through
this helper so the gateway receives the marker and permits the push in
concurrent mode.

The actual ``git push`` happens inside the gateway process, which holds
the GitHub App credentials.  The agent's sandbox does not authenticate
to origin directly; this helper only hits ``POST /api/v1/git/push`` on
the gateway sidecar.

Extracted from ``sandbox/egg_lib/orch_cli.py:_consensus_push`` for #1994
so MCP-only agents (which cannot shell out to the CLI) can publish BRC
artifacts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def consensus_push() -> tuple[int, str | None]:
    """Push code via the gateway with the ``consensus_push`` marker set.

    Calls the gateway push API directly (instead of ``git push``) so the
    ``consensus_push`` flag is included in the JSON payload.  This lets
    the gateway distinguish consensus-protocol pushes from direct pushes
    in concurrent mode.

    When ``GATEWAY_URL`` is unset (local development, no gateway
    running) we fall back to plain ``git push`` — concurrent-mode
    enforcement does not apply in that path because the gateway isn't
    present to enforce it.

    Returns ``(0, None)`` on success or ``(1, error_message)`` on failure.
    The error message includes the specific reason so MCP callers (where
    stderr is not visible to the agent) can surface it in HandlerError.
    """
    # Late import to avoid any risk of circular import between the
    # ``egg_lib`` CLI layer and the ``egg_agent_tools`` MCP layer.
    from egg_lib.cli_push import _retarget_refspec

    repo_path = os.environ.get("EGG_REPO_PATH", "")
    gateway_url = os.environ.get("GATEWAY_URL", "")
    session_token = os.environ.get("EGG_SESSION_TOKEN", "")
    container_id = os.environ.get("CONTAINER_ID", "")

    if not gateway_url:
        # Fallback: plain git push when the gateway is not reachable
        # (e.g. local development).  No concurrent-mode enforcement
        # exists in this path.
        try:
            subprocess.check_output(
                ["git", "push"],
                text=True,
                cwd=repo_path or None,
                stderr=subprocess.STDOUT,
            )
            return 0, None
        except subprocess.CalledProcessError as e:
            msg = f"git push failed: {e.output.strip()}"
            print(f"Error: {msg}", file=sys.stderr)
            return 1, msg
        except FileNotFoundError:
            msg = "git not found"
            print(f"Error: {msg}", file=sys.stderr)
            return 1, msg

    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            text=True,
            cwd=repo_path or None,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError, FileNotFoundError:
        branch = ""

    payload_data: dict[str, object] = {
        "repo_path": repo_path,
        "remote": "origin",
        "force": False,
        "container_id": container_id,
        "consensus_push": True,
    }

    if branch:
        # Retarget to the assigned pipeline branch when on a per-agent work
        # branch (shared logic with ``cli_push``).
        retarget = _retarget_refspec(branch)
        if retarget:
            refspec = retarget
        else:
            try:
                tracking = subprocess.check_output(
                    ["git", "config", f"branch.{branch}.merge"],
                    text=True,
                    cwd=repo_path or None,
                    stderr=subprocess.DEVNULL,
                ).strip()
                remote_branch = tracking.removeprefix("refs/heads/")
                refspec = f"{branch}:{remote_branch}" if remote_branch != branch else branch
            except subprocess.CalledProcessError, FileNotFoundError:
                refspec = branch
        payload_data["refspec"] = refspec
    else:
        # Detached HEAD (post-rebase, after manual checkout-by-sha, etc.).
        # Push by SHA instead — the gateway resolves the assigned branch
        # from the session and constructs the refspec server-side. See
        # issue #2200: when the worktree was on detached HEAD the helper
        # used to bail with "could not determine current branch", trapping
        # the agent with no way to publish its BRC proposal.
        try:
            commit_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                text=True,
                cwd=repo_path or None,
                stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError, FileNotFoundError:
            commit_sha = ""
        if not commit_sha:
            msg = "could not determine HEAD commit for push"
            print(f"Error: {msg}", file=sys.stderr)
            return 1, msg
        payload_data["commit_sha"] = commit_sha

    payload = json.dumps(payload_data).encode()

    req = urllib.request.Request(
        f"{gateway_url}/api/v1/git/push",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {session_token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
            stdout = body.get("data", {}).get("stdout", "")
            stderr = body.get("data", {}).get("stderr", "")
            if stdout:
                print(stdout)
            if stderr:
                print(stderr, file=sys.stderr)
            return 0, None
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            msg = body.get("message", "Unknown error")
            details = body.get("data", {})
        except Exception:
            msg = f"HTTP {e.code}"
            details = {}
        detail_str = f" ({json.dumps(details)})" if details else ""
        full_msg = f"git push failed: {msg}{detail_str}"
        print(f"Error: {full_msg}", file=sys.stderr)
        return 1, full_msg
    except urllib.error.URLError as e:
        msg = f"gateway unreachable: {e.reason}"
        print(f"Error: {msg}", file=sys.stderr)
        return 1, msg


__all__ = ["consensus_push"]
