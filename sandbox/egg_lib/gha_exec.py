"""GitHub Actions entry point for the egg sandbox container.

This module exists as a dedicated home for the ``gha_exec()`` function
previously defined in ``sandbox/egg_lib/cli.py``.  ``cli.py`` used to host
both the interactive-mode ``main()`` entry point AND this GHA-only entry
point; interactive mode was removed in #1762 and the remaining
``gha_exec()`` function was relocated here so the GHA action can continue
to import it without pulling in the dead-code ``main()`` path.

The GHA action shell script (``action/entrypoint.sh``) calls this
function via:

    python3 -c "from egg_lib.gha_exec import gha_exec; import sys; sys.exit(gha_exec())"

The function signature and behaviour are byte-identical to the prior
``egg_lib.cli.gha_exec`` implementation.
"""

from __future__ import annotations

import os

from .context import RuntimeContext, set_context
from .output import error, info, set_quiet_mode
from .runtime import exec_in_new_container


def gha_exec() -> int:
    """Entry point for GitHub Actions — called by ``action/entrypoint.sh``.

    Reads configuration from ``EGG_*`` environment variables (set by the
    action shell script) and orchestrates the full GHA flow:

    1. Build ``RuntimeContext`` from environment
    2. Create networks (dynamic subnet allocation)
    3. Start gateway container (pre-built image)
    4. Detect mode from ``GITHUB_EVENT_REPOSITORY_VISIBILITY``
    5. Build claude command from ``INPUT_PROMPT``, ``INPUT_MODEL``, etc.
    6. Execute in ephemeral container via ``exec_in_new_container()``
    7. Cleanup (ephemeral flag triggers gateway + network teardown)

    Returns:
        Exit code (0 = success)
    """
    from .docker import ensure_gateway_networks
    from .gateway import start_gateway_container as start_gw

    # 1. Build context from environment
    ctx = RuntimeContext.from_environment()
    set_context(ctx)

    # Verbose output for CI logs
    set_quiet_mode(False)

    info("GHA exec: starting orchestration")
    info(f"  gateway_image={ctx.gateway_image}")
    info(f"  sandbox_image={ctx.sandbox_image}")
    info(f"  isolated_network={ctx.isolated_network}")
    info(f"  external_network={ctx.external_network}")
    info(f"  ephemeral={ctx.ephemeral}")

    # 2. Create networks (dynamic subnets when "auto")
    if not ensure_gateway_networks():
        error("Failed to create gateway networks")
        return 1

    # 3. Start gateway container
    if not start_gw():
        error("Failed to start gateway container")
        return 1

    # 4. Detect mode
    mode_input = os.environ.get("INPUT_MODE", "auto")
    if mode_input == "auto":
        repo_vis = os.environ.get("GITHUB_EVENT_REPOSITORY_VISIBILITY", "public")
        mode = "private" if repo_vis in ("private", "internal") else "public"
        info(f"Auto-detected mode: {mode} (visibility={repo_vis})")
    else:
        mode = mode_input
        info(f"Configured mode: {mode}")

    # 5. Build claude command
    prompt = os.environ.get("INPUT_PROMPT", "")
    model = os.environ.get("INPUT_MODEL", "opus[1m]")
    timeout = int(os.environ.get("INPUT_TIMEOUT", "30"))

    if not prompt.strip():
        error(
            "INPUT_PROMPT is required for GHA exec mode. "
            "Long-running agents (e.g. overseer) should use the Agent SDK "
            "via build_agent_command() instead of claude --print."
        )
        return 1

    # --max-turns 200: Ensure agent has enough turns to complete work and post
    # comments. Default (100) was observed to be insufficient for tasks requiring
    # codebase exploration + implementation + testing + comment posting.
    command = [
        "claude",  # noqa: EGG100 - GHA exec entry point for one-shot prompts
        "--dangerously-skip-permissions",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--max-turns",
        "200",
        prompt,
    ]

    # 6. Execute
    # Build extra env for container (e.g., EGG_BOT_NAME for review markers)
    extra_env: dict[str, str] = {}
    bot_name = os.environ.get("EGG_BOT_NAME")
    if bot_name:
        extra_env["EGG_BOT_NAME"] = bot_name

    # Pass issue number so egg-contract CLI can find the contract
    issue_number = os.environ.get("EGG_ISSUE_NUMBER")
    if issue_number:
        extra_env["EGG_ISSUE_NUMBER"] = issue_number

    # Pass commit SHA so the gh wrapper can pin the review marker to the
    # commit that was actually checked out, avoiding races with new pushes
    commit_sha = os.environ.get("EGG_COMMIT_SHA")
    if commit_sha:
        extra_env["EGG_COMMIT_SHA"] = commit_sha

    # Pass agent role for gateway authorization (e.g., reviewer role)
    agent_role = os.environ.get("EGG_AGENT_ROLE")
    if agent_role:
        extra_env["EGG_AGENT_ROLE"] = agent_role

    # Pass PR number for checkpoint linkage (set by GHA review workflows)
    pr_number = os.environ.get("EGG_PR_NUMBER")
    if pr_number:
        extra_env["EGG_PR_NUMBER"] = pr_number

    # Pass pipeline ID for checkpoint correlation (set by orchestrator)
    pipeline_id = os.environ.get("EGG_PIPELINE_ID")
    if pipeline_id:
        extra_env["EGG_PIPELINE_ID"] = pipeline_id

    success_flag = exec_in_new_container(
        command=command,
        timeout_minutes=timeout,
        auth_mode="oauth-token",
        repo_mode=mode,
        extra_env=extra_env if extra_env else None,
    )

    return 0 if success_flag else 1
