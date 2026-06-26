"""``egg-orch session-state pull|push`` — cross-pod warm-resume sync (#3278).

Thin CLI layer over :mod:`egg_lib.session_state_sync`: resolves the
``(pipeline, slice, role)`` identity from env, talks to the orchestrator's
``/session-state`` route, and round-trips the transcript + pointer through the
pod's ephemeral Claude session store. The event-pump wrapper calls ``pull``
before invoking the agent and ``push`` after.

Best-effort by contract: every failure (no orchestrator, miss, bad payload, I/O
error) prints a diagnostic and exits ``0`` so the wrapper's flow continues — a
failed sync degrades to a safe cold reseed, never a wedged event.
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlencode

from egg_lib import session_state_sync


def _identity() -> tuple[str, str, str | None] | None:
    """Resolve ``(pipeline_id, role, slice_id)`` from env, or ``None`` if incomplete."""
    pipeline_id = os.environ.get("EGG_PIPELINE_ID")
    role = os.environ.get("EGG_AGENT_ROLE")
    if not pipeline_id or not role:
        print(
            "session-state: EGG_PIPELINE_ID and EGG_AGENT_ROLE required; skipping",
            file=sys.stderr,
        )
        return None
    slice_id = os.environ.get("EGG_SLICE_ID") or None
    return pipeline_id, role, slice_id


def _session_state_file(args: argparse.Namespace) -> str | None:
    return getattr(args, "session_state_file", None) or os.environ.get("EGG_SESSION_STATE_FILE")


def cmd_session_state_pull(args: argparse.Namespace) -> int:
    """Fetch the prior session for this (pipeline, slice, role) and stage a resume."""
    ident = _identity()
    if ident is None:
        return 0
    pipeline_id, role, slice_id = ident
    ssf = _session_state_file(args)
    if not ssf:
        print(
            "session-state pull: no --session-state-file / EGG_SESSION_STATE_FILE; skipping",
            file=sys.stderr,
        )
        return 0
    repo_path = session_state_sync.resolve_repo_path(getattr(args, "repo_path", None))
    config_dir = session_state_sync.resolve_config_dir(getattr(args, "config_dir", None))

    params = {"role": role}
    if slice_id:
        params["slice_id"] = slice_id
    endpoint = f"/api/v1/pipelines/{pipeline_id}/session-state?{urlencode(params)}"

    try:
        from egg_lib.orch_cli import orch_request

        result = orch_request(endpoint, method="GET")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — best-effort; never wedge the wrapper
        print(
            f"session-state pull: orchestrator request failed ({exc}); cold-starting",
            file=sys.stderr,
        )
        return 0

    if not isinstance(result, dict) or not result.get("found"):
        print("session-state pull: no prior session; cold-starting", file=sys.stderr)
        return 0

    record = result.get("data") or {}
    resumed = session_state_sync.write_pulled_state(
        record,
        repo_path=repo_path,
        config_dir=config_dir,
        session_state_file=ssf,
    )
    print(
        f"session-state pull: {'staged resume' if resumed else 'pointer only (cold-start)'} "
        f"for {role} slice={slice_id or 'none'}",
        file=sys.stderr,
    )
    return 0


def cmd_session_state_push(args: argparse.Namespace) -> int:
    """Persist this pod's post-run session (pointer + transcript) to the orchestrator."""
    ident = _identity()
    if ident is None:
        return 0
    pipeline_id, role, slice_id = ident
    ssf = _session_state_file(args)
    if not ssf:
        print(
            "session-state push: no --session-state-file / EGG_SESSION_STATE_FILE; skipping",
            file=sys.stderr,
        )
        return 0
    repo_path = session_state_sync.resolve_repo_path(getattr(args, "repo_path", None))
    config_dir = session_state_sync.resolve_config_dir(getattr(args, "config_dir", None))

    body = session_state_sync.read_state_for_push(
        repo_path=repo_path,
        config_dir=config_dir,
        session_state_file=ssf,
    )
    if body is None:
        print("session-state push: no session to persist; skipping", file=sys.stderr)
        return 0
    body["role"] = role
    body["slice_id"] = slice_id

    try:
        from egg_lib.orch_cli import orch_request

        orch_request(f"/api/v1/pipelines/{pipeline_id}/session-state", method="POST", data=body)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — best-effort; never wedge the wrapper
        print(
            f"session-state push: orchestrator request failed ({exc}); state not persisted",
            file=sys.stderr,
        )
        return 0
    print(
        f"session-state push: persisted {role} slice={slice_id or 'none'} "
        f"(transcript={'yes' if body.get('transcript') else 'no'})",
        file=sys.stderr,
    )
    return 0


def register_session_state_subcommand(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the ``session-state`` subcommand group on the given subparsers."""
    ss_parser = subparsers.add_parser(
        "session-state",
        help="Cross-pod warm-resume session sync (#3278).",
    )
    ss_sub = ss_parser.add_subparsers(dest="session_state_command")

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--session-state-file",
            dest="session_state_file",
            default=None,
            help="Pointer file path (default: $EGG_SESSION_STATE_FILE).",
        )
        p.add_argument(
            "--repo-path",
            dest="repo_path",
            default=None,
            help="Agent cwd for the Claude project slug (default: $EGG_REPO_PATH or cwd).",
        )
        p.add_argument(
            "--config-dir",
            dest="config_dir",
            default=None,
            help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude).",
        )

    pull_parser = ss_sub.add_parser("pull", help="Fetch prior session and stage a resume.")
    _add_common(pull_parser)
    pull_parser.set_defaults(func=cmd_session_state_pull)

    push_parser = ss_sub.add_parser("push", help="Persist this pod's session.")
    _add_common(push_parser)
    push_parser.set_defaults(func=cmd_session_state_push)

    ss_parser.set_defaults(func=cmd_session_state_pull)
