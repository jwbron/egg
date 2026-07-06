"""Gateway health cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Response, has_request_context, jsonify, request

try:
    from ..confluence_credentials import (
        reload_confluence_credentials,
    )
    from ..confluence_policy import (
        reload_confluence_policy,
    )
    from ..jira_credentials import (
        reload_jira_credentials,
    )
    from ..jira_policy import (
        reload_jira_policy,
    )
    from ..policy import (
        reload_policy_caches,
    )
except ImportError:  # flat/container import mode
    from confluence_credentials import (  # type: ignore[no-redef, import-untyped]
        reload_confluence_credentials,
    )
    from confluence_policy import (  # type: ignore[no-redef, import-untyped]
        reload_confluence_policy,
    )
    from jira_credentials import (  # type: ignore[no-redef, import-untyped]
        reload_jira_credentials,
    )
    from jira_policy import (  # type: ignore[no-redef, import-untyped]
        reload_jira_policy,
    )
    from policy import (  # type: ignore[no-redef, import-untyped]
        reload_policy_caches,
    )


def _b() -> Any:
    """Return the gateway barrel for call-time lookup of patched symbols.

    Seam getters/validators and gateway-local helpers are patched by tests at
    ``gateway.gateway.<name>``; resolving them on the barrel at call time keeps
    those patches effective after the split.
    """
    import sys

    return sys.modules.get("gateway.gateway") or sys.modules["gateway"]


class _BarrelLogger:
    """Proxy to the barrel ``logger`` so tests patching ``gateway.logger``
    observe log calls emitted from this submodule."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_b().logger, name)


logger: Any = _BarrelLogger()


def get_proxy_ca_cert() -> tuple[Response, int] | Response:
    """Serve the gateway proxy CA certificate (no auth required).

    The CA is public key material — every sandbox already receives it in
    its trust store via the shared-certs volume in Compose mode. Under
    k8s the agent Job ``command`` overrides the image ENTRYPOINT, so the
    sandbox's ``setup_gateway_ca()`` never runs and no shared volume is
    mounted; the one-shot event wrapper
    (``orchestrator/consensus_wrapper.py``, #3459) fetches the current
    CA from this endpoint per spawn and exports ``NODE_EXTRA_CA_CERTS``,
    so agents validate TLS-bumped hosts (e.g. the GitHub Packages npm
    read-through, #3456) without hand-wiring. Ad-hoc clients outside an
    agent pod fetch the same way::

        curl -sf "$GATEWAY_URL/api/v1/proxy/ca-cert" -o /tmp/gateway-ca.crt
        NODE_EXTRA_CA_CERTS=/tmp/gateway-ca.crt pnpm install ...

    Serving it per-request also stays correct across gateway restarts,
    which regenerate the CA (generate-ca-cert.sh).
    """
    ca_path = Path("/etc/squid/certs/gateway-ca.crt")
    if not ca_path.is_file():
        return jsonify({"error": "ca_cert_unavailable"}), 404
    return Response(ca_path.read_text(), mimetype="application/x-pem-file")


def health_check() -> Response:
    """Health check endpoint (no auth required)."""
    github = _b().get_github_client()
    token_valid = github.is_token_valid()

    # Check launcher secret is configured
    try:
        _b().get_launcher_secret()
        launcher_secret_configured = True
    except _b().LauncherSecretNotConfiguredError:
        launcher_secret_configured = False

    # Get session manager stats
    session_manager = _b().get_session_manager()
    active_sessions = len(session_manager.list_sessions())

    # Check orchestrator connectivity (if configured)
    orchestrator_status = _b()._check_orchestrator_connectivity()

    # Check Squid proxy health
    squid_status = _b()._check_squid_health()

    # Gateway always runs with locked Squid.
    # Per-container mode is enforced at container start via network selection.
    # - Private containers: isolated network + proxy (locked to api.anthropic.com)
    # - Public containers: external network + direct internet (no proxy)
    #
    # Status is "degraded" if Squid is down - private containers will be unable
    # to reach the internet. Previously invisible because health check only
    # verified the Python gateway (port 9848), not Squid (port 3129).
    # See: https://github.com/jwbron/egg/issues/1387
    is_healthy = token_valid and launcher_secret_configured and squid_status["listening"]

    # Record this observation so the snapshot can expose transitions (see #1855).
    _b()._health_tracker.record(is_healthy)
    tracker_snapshot = _b()._health_tracker.snapshot()

    response_data: dict[str, Any] = {
        "status": "healthy" if is_healthy else "degraded",
        "github_token_valid": token_valid,
        "auth_configured": launcher_secret_configured,
        "squid_proxy": squid_status,
        "active_sessions": active_sessions,
        "service": "gateway",
        "client_ip": request.remote_addr,
        "process_start_time": tracker_snapshot["process_start_time"],
        "healthy_since": tracker_snapshot["healthy_since"],
        "last_unhealthy_at": tracker_snapshot["last_unhealthy_at"],
        "recent_transitions": tracker_snapshot["recent_transitions"],
    }

    # Include orchestrator status if configured
    if orchestrator_status.get("configured"):
        response_data["orchestrator"] = orchestrator_status

    return jsonify(response_data)


def _reload_all_config() -> None:
    """Reload all cached configuration from disk/environment.

    Called by the SIGHUP handler and the /api/v1/config/reload endpoint.

    Thread safety: all cached values are immutable types (frozenset, tuple,
    None) and global variable assignment is atomic under CPython's GIL, so
    concurrent readers see either the old or new value, never a torn state.
    Avoid replacing any cache with a mutable type (e.g. dict) without adding
    synchronisation.
    """
    try:
        from config.repo_config import reload_config as reload_repo_config
    except ImportError:
        try:
            from repo_config import reload_config as reload_repo_config  # type: ignore[no-redef]
        except ImportError:
            reload_repo_config = None  # type: ignore[assignment]

    if reload_repo_config is not None:
        try:
            reload_repo_config()
        finally:
            reload_policy_caches()
        logger.info("Configuration reloaded")
    else:
        reload_policy_caches()
        logger.warning("Policy caches reloaded (repo_config unavailable)")

    # Jira credentials + project allowlist — both sit on disk next to the
    # other gateway config, so a single ``POST /api/v1/config/reload`` should
    # refresh them alongside the GitHub policy caches.  Failing the Jira
    # reload must not tank the endpoint (operators may be running without
    # Jira configured), so we log and continue.
    try:
        reload_jira_credentials()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Jira credentials reload failed")
    try:
        reload_jira_policy()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Jira project allowlist reload failed")
    # ``_reload_all_config`` is reachable from two call sites: (a) the
    # ``POST /api/v1/config/reload`` endpoint, which runs inside a Flask
    # request; and (b) the SIGHUP handler, which does NOT.  ``audit_log``
    # dereferences ``request.remote_addr`` so calling it outside a request
    # raises ``RuntimeError: Working outside of request context``.  Gate
    # the audit on ``has_request_context`` so HTTP reloads still audit and
    # SIGHUP falls back to a bare logger line.
    if has_request_context():
        _b().audit_log(
            "jira_config_reloaded",
            "config_reload",
            success=True,
            details={"components": ["jira_credentials", "jira_policy"]},
        )
    else:
        logger.info(
            "Jira configuration reloaded",
            components=["jira_credentials", "jira_policy"],
            trigger="sighup",
        )

    # Confluence credentials + space allowlist — same disk-cache pattern as
    # Jira.  The Confluence allowlist lives under the ``confluence:`` section
    # of context-filters.yaml; credentials share the secrets.env file.
    try:
        reload_confluence_credentials()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Confluence credentials reload failed")
    try:
        reload_confluence_policy()
    except Exception:  # pragma: no cover — defensive
        logger.exception("Confluence space allowlist reload failed")
    if has_request_context():
        _b().audit_log(
            "confluence_config_reloaded",
            "config_reload",
            success=True,
            details={"components": ["confluence_credentials", "confluence_policy"]},
        )
    else:
        logger.info(
            "Confluence configuration reloaded",
            components=["confluence_credentials", "confluence_policy"],
            trigger="sighup",
        )


def config_reload() -> Response:
    """Reload configuration from disk.

    Clears all in-memory config caches so the next access re-reads from
    repositories.yaml and environment variables. Requires launcher auth.
    """
    _b()._reload_all_config()
    return jsonify({"status": "ok", "message": "Configuration reloaded"})
