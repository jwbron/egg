"""Environment, git, gateway-CA, and Anthropic-API setup."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from egg_config import GATEWAY_PORT

from ._config import Config, Logger
from ._core import run_cmd


def setup_environment(config: Config) -> None:
    """Set up environment variables."""
    os.environ["HOME"] = str(config.user_home)
    os.environ["USER"] = config.container_user

    # Add user's local bin (Claude Code native install) to PATH.
    # Note: /opt/egg-runtime/sandbox/bin is already on PATH via the image-level ENV
    # directive in Dockerfile (see issue #1799), so we don't re-add it here.
    current_path = os.environ.get("PATH", "")
    local_bin = config.user_home / ".local" / "bin"
    os.environ["PATH"] = f"{local_bin}:{current_path}"

    # Python settings
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONUNBUFFERED"] = "1"

    # Claude settings
    os.environ["DISABLE_AUTOUPDATER"] = "1"

    # Set EGG_REPO_PATH if not already provided (orchestrator sets it, CLI doesn't)
    if "EGG_REPO_PATH" not in os.environ:
        os.environ["EGG_REPO_PATH"] = str(config.user_home / "repos")

    # EGG_PIPELINE_REPO is the GitHub-style "owner/repo" string the
    # overseer auto-issue verb (issue #1962) and the gateway's
    # overseer guardrails rely on. The orchestrator MUST inject it
    # for every spawned sandbox running the overseer role; we
    # fail-fast (no `gh repo view` fallback) because a misconfigured
    # pipeline filing an issue against the wrong repo is worse than a
    # hard abort here. Skipped for the local CLI flow which doesn't
    # run the overseer.
    if "EGG_PIPELINE_REPO" not in os.environ and os.environ.get("EGG_AGENT_ROLE") in {"overseer"}:
        # Structured stderr line so operators can grep for the symptom.
        # We don't have access to the structured Logger here (setup_environment
        # runs before logger is wired through), so write straight to stderr.
        import sys as _sys

        print(
            "entrypoint: missing required env EGG_PIPELINE_REPO; "
            "orchestrator must inject it for the overseer role. "
            "Refusing to continue.",
            file=_sys.stderr,
        )
        raise OSError(
            "EGG_PIPELINE_REPO env var is required for the overseer role; "
            "orchestrator must inject it. Refusing to continue."
        )

    # Git editor - use 'true' (no-op) for non-interactive environment
    # This allows git rebase --continue to work without an interactive editor.
    # Side effects: git commit without -m creates empty messages, git rebase -i
    # applies default picks. This is intentional for autonomous operation.
    os.environ["GIT_EDITOR"] = "true"


def setup_git(config: Config, logger: Logger) -> None:
    """Configure git for egg identity and credential helper."""
    user_tuple = (config.runtime_uid, config.runtime_gid)

    # Set git identity — include agent role for auditability in multi-agent pipelines
    agent_role = os.environ.get("EGG_AGENT_ROLE", "")
    if agent_role:
        git_name = f"egg ({agent_role})"
        git_email = f"{agent_role}@egg.local"
    else:
        git_name = "egg"
        git_email = "egg@localhost"
    run_cmd(["git", "config", "--global", "user.name", git_name], as_user=user_tuple)
    run_cmd(["git", "config", "--global", "user.email", git_email], as_user=user_tuple)

    # Configure credential helper if token available
    if config.github_token:
        run_cmd(
            [
                "git",
                "config",
                "--global",
                "credential.helper",
                "/opt/egg-runtime/sandbox/bin/git-credential-github-token",
            ],
            as_user=user_tuple,
        )
        run_cmd(
            ["git", "config", "--global", "credential.useHttpPath", "true"],
            as_user=user_tuple,
        )
        logger.success("Git credential helper configured for GitHub push")
    else:
        run_cmd(["git", "config", "--global", "credential.helper", ""], as_user=user_tuple)

    # Never embed tokens in URLs
    run_cmd(
        ["git", "config", "--global", "advice.pushUpdateRejected", "false"],
        as_user=user_tuple,
    )

    logger.success(f"Git configured to commit as {git_name} <{git_email}>")


def setup_gateway_ca(config: Config, logger: Logger) -> None:
    """Add gateway CA certificate to container trust store.

    Note: With ANTHROPIC_BASE_URL routing Claude Code traffic directly to the
    gateway HTTP endpoint (PR #701), this CA trust is no longer required for
    Anthropic API traffic. The Squid proxy now only does peek/splice (SNI
    inspection without MITM), so clients validate origin server certificates
    directly.

    This function is kept for:
    1. Backwards compatibility during transition
    2. Potential future HTTPS interception needs (if we ever need to MITM
       other traffic through the proxy)

    The CA cert is copied from the shared volume (populated by gateway
    entrypoint) to the system CA store.

    Note on idempotency: update-ca-certificates is idempotent and can
    be called multiple times safely.
    """
    gateway_ca_src = Path("/shared/certs/gateway-ca.crt")
    gateway_ca_dst = Path("/usr/local/share/ca-certificates/gateway-ca.crt")

    if not gateway_ca_src.exists():
        # With ANTHROPIC_BASE_URL, missing CA is not a critical error
        # (Anthropic traffic goes directly to gateway HTTP endpoint)
        logger.info("Gateway CA certificate not found (not required with ANTHROPIC_BASE_URL)")
        return

    # Copy cert to ca-certificates directory
    shutil.copy(gateway_ca_src, gateway_ca_dst)
    gateway_ca_dst.chmod(0o644)

    # Update system trust store
    result = run_cmd(["update-ca-certificates"], check=False, capture=True)
    if result.returncode == 0:
        logger.success("Gateway CA certificate added to trust store")
    else:
        # Not critical with ANTHROPIC_BASE_URL - just log info
        logger.info(f"Gateway CA not added to trust store: {result.stderr}")

    # Configure Python and Node.js to use system CA bundle
    # Python's requests library uses certifi by default, not the system store
    # Node.js needs NODE_EXTRA_CA_CERTS for additional CAs
    system_ca_bundle = "/etc/ssl/certs/ca-certificates.crt"
    os.environ["REQUESTS_CA_BUNDLE"] = system_ca_bundle
    os.environ["SSL_CERT_FILE"] = system_ca_bundle
    os.environ["NODE_EXTRA_CA_CERTS"] = str(gateway_ca_dst)


def setup_anthropic_api(config: Config, logger: Logger) -> None:
    """Configure Anthropic API to route through gateway for credential injection.

    Sets ANTHROPIC_BASE_URL to route Claude Code API calls through the gateway,
    where credentials are injected. This approach:
    - Uses Claude Code's documented ANTHROPIC_BASE_URL configuration
    - No SSL MITM needed for Anthropic traffic (HTTP to gateway, HTTPS to API)
    - Credentials never exist in container environment
    - Works for both API key and OAuth modes

    A placeholder OAuth token is set to satisfy Claude Code's startup validation.
    The gateway strips this placeholder and injects real credentials.

    Security note: when ``EGG_SESSION_TOKEN`` is set (the k8s/Compose
    orchestrator path), the placeholder envelope embeds the session
    token verbatim. The token therefore appears in *two* env vars
    inside the sandbox — ``EGG_SESSION_TOKEN`` and
    ``CLAUDE_CODE_OAUTH_TOKEN``. Net attack surface is unchanged (any
    in-sandbox process that could read one could already read the
    other) and Claude Code requires the placeholder in this env var,
    but readers grepping for the session secret should know it lives
    in both places.

    Reference: PR #701 - ANTHROPIC_BASE_URL credential injection plan;
    issue #2829 - token-keyed session lookup in /v1/messages proxy.
    """
    gateway_url = os.environ.get("GATEWAY_URL", f"http://egg-gateway:{GATEWAY_PORT}")

    # Placeholder credential to satisfy Claude Code's startup validation.
    # Gateway strips this and injects real credentials.
    #
    # When EGG_SESSION_TOKEN is present (k8s + Compose orchestrator paths)
    # the placeholder wraps the session token so the gateway's /v1/messages
    # proxy can identify the session from the request header rather than
    # falling back to ephemeral pod-IP lookup (issue #2829). The static
    # fallback covers dev/host flows where no session has been registered.
    session_token = os.environ.get("EGG_SESSION_TOKEN")
    if session_token:
        from egg_session_placeholder import to_placeholder

        placeholder = to_placeholder(session_token)
    else:
        placeholder = (
            "sk-ant-oat01-PROXY-INJECTED-gateway-handles-real-credential-"
            "00000000000000000000000000000000000000000000000000000000000000-000000AAAA"
        )

    # Set ANTHROPIC_BASE_URL to route API calls through gateway
    os.environ["ANTHROPIC_BASE_URL"] = gateway_url

    # On the LiteLLM path (ANTHROPIC_CUSTOM_MODEL_OPTION set), Claude Code
    # uses api_key auth and sends credentials via the x-api-key header — set
    # ANTHROPIC_API_KEY so the session token reaches the gateway and the
    # entrypoint doesn't skip OAuth thinking it needs the api-key flow.
    # On the Anthropic path, Claude Code uses OAuth and sends via the
    # Anthropic-Header — set CLAUDE_CODE_OAUTH_TOKEN instead.
    # Both headers reach the gateway's x-api-key extraction (gateway.py:9564),
    # so LiteLLM-routed requests carry the same session envelope.
    is_litellm_path = bool(os.environ.get("ANTHROPIC_CUSTOM_MODEL_OPTION"))
    if is_litellm_path:
        os.environ["ANTHROPIC_API_KEY"] = placeholder
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    else:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = placeholder
        os.environ.pop("ANTHROPIC_API_KEY", None)

    logger.success(f"Anthropic API routed through gateway: {gateway_url}")
    logger.info(
        f"  Credentials injected by gateway (not in container), "
        f"path={'litellm' if is_litellm_path else 'anthropic'}"
    )
