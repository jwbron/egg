"""Gateway server cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from typing import Any


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


def _run_health_server(host: str, port: int) -> None:
    """Run a dedicated lightweight HTTP server for health checks.

    This server runs on a separate port from the main Waitress thread pool,
    ensuring health checks are never blocked by long-running API requests
    (e.g., synchronous git operations holding Waitress threads).
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/api/v1/health":
                self.send_response(404)
                self.end_headers()
                return

            # Lightweight health check for Docker liveness probes.
            # Note: is_token_valid() can block during token refresh (up to 30s
            # synchronous HTTP call to GitHub). ThreadingHTTPServer ensures a
            # slow refresh doesn't block concurrent health check requests.
            # The full health endpoint on the main port still does
            # orchestrator/squid process checks for detailed diagnostics.
            try:
                github = _b().get_github_client()
                token_valid = github.is_token_valid()
            except Exception:
                token_valid = False

            try:
                _b().get_launcher_secret()
                launcher_ok = True
            except Exception:
                launcher_ok = False

            # Quick squid port check
            squid_listening = False
            try:
                with socket.create_connection(("127.0.0.1", 3129), timeout=2):
                    squid_listening = True
            except OSError:
                pass

            is_healthy = token_valid and launcher_ok and squid_listening
            body = json.dumps(
                {
                    "status": "healthy" if is_healthy else "degraded",
                    "github_token_valid": token_valid,
                    "auth_configured": launcher_ok,
                    "squid_proxy": {"listening": squid_listening},
                    "service": "gateway",
                }
            ).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress default stderr logging for health checks
            pass

    server = ThreadingHTTPServer((host, port), HealthHandler)
    server.serve_forever()


def main() -> None:
    """Run the gateway server."""
    # Safety check: refuse to run as root to prevent permission issues
    # When the gateway runs as root, git objects are created with root:root ownership,
    # which breaks git operations on the host (permission denied on .git/objects).
    if os.getuid() == 0:
        print(
            "ERROR: gateway must not run as root.\n"
            "\n"
            "Running as root causes git objects to be created with root:root ownership,\n"
            "which breaks git operations on the host with 'permission denied' errors.\n"
            "\n"
            "To fix this:\n"
            "  1. Check the service file path in gateway.service\n"
            "  2. Ensure the gateway is started via 'egg' or 'bin/egg-deploy up'\n"
            "  3. Restart the gateway and try again\n"
            "  4. Verify the gateway is running as your user: ps aux | grep gateway\n"
            "\n"
            "If .git/objects already has root-owned files, fix with:\n"
            "  sudo chown -R $(id -u):$(id -g) ~/repos/*/.git",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Gateway Sidecar REST API")
    parser.add_argument(
        "--host",
        default=_b().DEFAULT_HOST,
        help=f"Host to listen on (default: {_b().DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_b().DEFAULT_PORT,
        help=f"Port to listen on (default: {_b().DEFAULT_PORT})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=_b().DEFAULT_THREADS,
        help=f"Waitress thread pool size (default: {_b().DEFAULT_THREADS})",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=_b().HEALTH_CHECK_PORT,
        help=f"Dedicated health check port (default: {_b().HEALTH_CHECK_PORT})",
    )

    args = parser.parse_args()

    # Initialize token refresher for in-memory token management.
    # Retry with backoff on transient failures (e.g. DNS not available at startup).
    # Without a GitHub token the gateway can't serve its purpose, so exit if
    # initialization never succeeds.
    token_init_timeout = int(os.environ.get("EGG_TOKEN_INIT_TIMEOUT", "120"))
    try:
        from token_refresher import (
            initialize_token_refresher,
            is_token_refresher_permanently_failed,
        )

        refresher = None
        start_time = time.time()
        attempt = 0
        while True:
            attempt += 1
            refresher = initialize_token_refresher()
            if refresher:
                logger.info("Token refresher initialized (in-memory token refresh enabled)")
                break

            # Permanent failures (missing credentials/key file) won't resolve
            # on retry — exit immediately instead of waiting for the timeout.
            if is_token_refresher_permanently_failed():
                logger.warning("Token refresher not configured - GitHub operations will fail")
                break

            elapsed = time.time() - start_time
            if elapsed >= token_init_timeout:
                logger.error(
                    "Token refresher failed to initialize after timeout — exiting",
                    timeout_seconds=token_init_timeout,
                    attempts=attempt,
                )
                sys.exit(1)

            backoff = min(5 * (2 ** (attempt - 1)), 30)
            remaining = token_init_timeout - elapsed
            wait = min(backoff, remaining)
            logger.warning(
                "Token refresher not ready, retrying",
                attempt=attempt,
                retry_in_seconds=wait,
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=token_init_timeout,
            )
            time.sleep(wait)
    except ImportError:
        logger.error("Token refresher module not available - GitHub operations will fail")
        sys.exit(1)
    except Exception as e:
        logger.error(
            "Token refresher initialization failed unexpectedly",
            error=str(e),
            error_type=type(e).__name__,
        )
        sys.exit(1)

    # Initialize reviewer token refresher (optional — for posting reviews with
    # approve/request-changes using a separate GitHub App identity).
    # Reviewer is optional so we don't retry or block startup.
    try:
        from token_refresher import initialize_reviewer_token_refresher

        reviewer_refresher = initialize_reviewer_token_refresher()
        if reviewer_refresher:
            logger.info("Reviewer token refresher initialized")
        else:
            logger.debug("Reviewer token refresher not configured (optional)")
    except ImportError:
        pass  # Already logged above
    except Exception as e:
        logger.warning("Reviewer token refresher initialization failed", error=str(e))

    # Validate user mode config if configured
    github = _b().get_github_client()
    is_valid, validation_msg = github.validate_user_mode_config()
    if not is_valid:
        logger.warning("User mode config validation failed", reason=validation_msg)
    else:
        logger.info("User mode config", status=validation_msg)

    # Load sessions BEFORE worktree cleanup so we know which containers are active.
    # After a gateway restart, Docker CLI may not be available inside the container,
    # so we derive the active container set from persisted sessions instead.
    #
    # Each session contributes its own ``container_id`` plus the per-agent
    # and pipeline-level worktree anchor IDs ({pipeline_id}-{role} and
    # {pipeline_id}).  Without those derived anchors, cleanup would treat
    # every live pipeline's per-agent worktree as orphaned because the
    # on-disk dir name never matches the session container_id (#1874).
    active_container_ids: set[str] = set()
    try:
        session_manager = _b().get_session_manager()
        pruned = session_manager.prune_expired_sessions()
        if pruned > 0:
            logger.info(f"Startup session cleanup pruned {pruned} expired session(s)")
        # Extract active container IDs from surviving sessions, plus the
        # per-agent/pipeline worktree anchors the orchestrator assigns.
        sessions = session_manager.list_sessions()
        active_container_ids |= _b()._container_ids_from_sessions(sessions)
        if active_container_ids:
            logger.info(
                "Active containers from sessions",
                count=len(active_container_ids),
            )
    except Exception as e:
        logger.warning("Startup session cleanup failed", error=str(e))

    # Also check Docker directly as safety net — sessions may be
    # pruned but containers still running.
    try:
        docker_containers = _b().get_active_docker_containers()
        active_container_ids |= docker_containers
    except Exception as e:
        logger.warning("Could not query Docker containers", error=str(e))

    # Clean up orphaned worktrees in a background thread so it doesn't block
    # the Waitress thread pool at startup. Worktree cleanup involves synchronous
    # git operations that can hold threads for seconds each, and with many
    # orphaned sessions this was exhausting the thread pool before the gateway
    # could serve any requests. See: https://github.com/jwbron/egg/issues/1400
    def _background_worktree_cleanup() -> None:
        try:
            # Container liveness alone cannot distinguish a crashed leftover
            # from a pipeline parked at a HITL gate (no containers, no
            # sessions — that's its normal state). Ask the orchestrator which
            # pipelines are live before sweeping; on a redeploy it may still
            # be booting, so poll up to the configured deadline. If it never
            # answers, startup_cleanup skips the sweep (fail-safe) — see
            # #3070, where a blind sweep with active_containers=0 deleted a
            # parked pipeline's worktree, contract, and branches.
            active_pipeline_ids = _b().wait_for_active_pipeline_ids()
            orphans_removed = _b().startup_cleanup(
                active_containers=active_container_ids,
                session_manager=_b().get_session_manager(),
                active_pipeline_ids=active_pipeline_ids,
            )
            if orphans_removed > 0:
                logger.info(f"Startup cleanup removed {orphans_removed} orphaned worktree(s)")
        except Exception as e:
            logger.warning("Startup worktree cleanup failed", error=str(e))

    cleanup_thread = threading.Thread(
        target=_background_worktree_cleanup,
        name="startup-worktree-cleanup",
        daemon=True,
    )
    cleanup_thread.start()

    # Start background session pruner so stale entries don't accumulate across
    # restarts.  Without this, sessions for dead containers survive until their
    # 24h TTL lapses and are reloaded on every gateway restart (#1884).
    try:
        prune_interval = max(1, int(os.environ.get("EGG_SESSION_CLEANUP_INTERVAL_MINUTES", "15")))
        idle_timeout = max(5, int(os.environ.get("EGG_SESSION_IDLE_TIMEOUT_MINUTES", "60")))
        _b().get_session_manager().start_background_pruner(
            interval_minutes=prune_interval,
            idle_timeout_minutes=idle_timeout,
        )
    except Exception as e:
        logger.warning("Failed to start session background pruner", error=str(e))

    # Ensure launcher secret is configured - fail startup if not
    try:
        _b().get_launcher_secret()
    except _b().LauncherSecretNotConfiguredError as e:
        logger.error("Startup failed: launcher secret not configured", error=str(e))
        sys.exit(1)

    # Under k8s the compose-era default hostname "egg-orchestrator" does
    # not resolve, so falling back to it produces cryptic "Orchestrator
    # unreachable" errors on the agent side mid-pipeline. Fail startup
    # instead so the misconfiguration is visible at deploy time (#1803).
    if os.environ.get("KUBERNETES_SERVICE_HOST") and not os.environ.get("EGG_ORCHESTRATOR_URL"):
        logger.error(
            "Startup failed: EGG_ORCHESTRATOR_URL must be set when running in Kubernetes. "
            "Set it on the gateway Deployment, e.g. "
            "http://orchestrator.egg-system.svc.cluster.local:9849"
        )
        sys.exit(1)

    # Register SIGHUP handler for config reload.
    # Usage: docker kill -s HUP egg-gateway
    def _handle_sighup(signum: int, frame: Any) -> None:
        _b()._reload_all_config()

    signal.signal(signal.SIGHUP, _handle_sighup)

    # Register SIGTERM handler for graceful shutdown.
    # When Docker sends SIGTERM, delay for 5s to let in-flight session cleanup
    # requests complete before exiting. This prevents the race condition where
    # the gateway becomes unreachable before the launcher's cleanup hook runs.
    # NOTE: This is a delay, not a true drain — waitress continues accepting new
    # requests during the sleep. If a new long-running request starts during this
    # window, it will be killed when sys.exit(0) fires. For our use case (Docker
    # stop), this is acceptable since the only in-flight requests at shutdown are
    # short-lived session cleanup calls.
    def _handle_shutdown(signum: int, frame: Any) -> None:
        logger.info("Received SIGTERM, delaying 5s for in-flight requests before shutdown...")
        time.sleep(5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info(
        "Starting Gateway Sidecar",
        host=args.host,
        port=args.port,
        debug=args.debug,
        threads=args.threads,
        health_port=args.health_port,
    )
    logger.info("Session authentication required for all container operations")

    # Optional tracemalloc sampler (opt-in via GATEWAY_MEM_TRACE=1). Emits
    # periodic RSS + top-allocation-site log records to stdout so the trail
    # survives pod OOM via `kubectl logs --previous`. See #1885.
    try:
        from ..mem_trace import start_if_enabled as _mem_trace_start
    except ImportError:
        from mem_trace import (  # type: ignore[no-redef, import-untyped]
            start_if_enabled as _mem_trace_start,
        )
    _mem_trace_start()

    # Start dedicated health check server on a separate port so Docker/orchestrator
    # health checks are never blocked by long-running git operations on the main
    # Waitress thread pool. See: https://github.com/jwbron/egg/issues/1400
    health_thread = threading.Thread(
        target=_run_health_server,
        args=(args.host, args.health_port),
        name="health-check-server",
        daemon=True,
    )
    health_thread.start()
    logger.info("Dedicated health check server started", port=args.health_port)

    # Run with production server in production, debug server in debug mode
    if args.debug:
        _b().app.run(host=args.host, port=args.port, debug=True)
    else:
        # Use waitress for production with configurable thread pool.
        # Increased from 8 (previous default) to 32 to handle concurrent load
        # from multiple SDLC pipelines. See: https://github.com/jwbron/egg/issues/1400
        _b().serve(_b().app, host=args.host, port=args.port, threads=args.threads)
