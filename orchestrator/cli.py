#!/usr/bin/env python3
"""
egg-orchestrator CLI.

Provides command-line interface for:
- Starting the orchestrator API server
- Managing pipelines
- Checking service health
- Debugging operations

Usage:
    egg-orchestrator serve [--host HOST] [--port PORT] [--debug]
    egg-orchestrator health
    egg-orchestrator pipelines list
    egg-orchestrator pipelines create --issue NUMBER --repo REPO
    egg-orchestrator pipelines status PIPELINE_ID
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import configure_logging, get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)

    def configure_logging(**kwargs) -> None:
        logging.basicConfig(level=logging.INFO)


try:
    from egg_config import ORCHESTRATOR_PORT
except ImportError:
    ORCHESTRATOR_PORT = 9849

logger = get_logger("orchestrator.cli")


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the orchestrator API server."""
    # Safety check: refuse to run as root to prevent permission issues.
    # When the orchestrator runs as root, git refs are created with root:root
    # ownership (e.g. refs/heads/egg/), which breaks git operations on the
    # host with 'permission denied' errors.  The entrypoint should drop
    # privileges via gosu before reaching here.
    if os.getuid() == 0:
        print(
            "ERROR: orchestrator must not run as root.\n"
            "\n"
            "Running as root causes git refs to be created with root:root ownership,\n"
            "which breaks git operations on the host with 'permission denied' errors.\n"
            "\n"
            "Ensure HOST_UID and HOST_GID are set so the entrypoint drops\n"
            "privileges via gosu before starting the orchestrator.\n"
            "\n"
            "If .git/refs already has root-owned files, fix with:\n"
            "  sudo chown -R $(id -u):$(id -g) ~/repos/*/.git/refs",
            file=sys.stderr,
        )
        sys.exit(1)

    from api import app

    host = args.host
    port = args.port
    debug = args.debug

    # Override from environment
    host = os.environ.get("ORCHESTRATOR_HOST", host)
    port = int(os.environ.get("ORCHESTRATOR_PORT", port))
    debug = os.environ.get("ORCHESTRATOR_DEBUG", "").lower() == "true" or debug

    logger.info(
        "Starting egg-orchestrator",
        host=host,
        port=port,
        debug=debug,
    )

    repo_path = os.environ.get("EGG_REPO_PATH", "not set")
    host_repo_map = os.environ.get("EGG_HOST_REPO_MAP", "not set")
    logger.info(
        "Configuration",
        repo_path=repo_path,
        host_repo_map=host_repo_map,
    )

    if repo_path != "not set":
        try:
            from docker_client import get_docker_client
            from startup_reconciliation import reconcile_stale_containers
            from state_store import get_state_store

            recovered = reconcile_stale_containers(get_state_store(repo_path), get_docker_client())
            if recovered:
                logger.warning("Recovered stale pipelines on startup", count=recovered)
        except Exception as reconcile_err:
            logger.warning(
                "Startup reconciliation failed",
                error=str(reconcile_err),
            )

        try:
            from container_monitor import (
                create_pipeline_reconciliation_handler,
                get_container_monitor,
            )

            monitor = get_container_monitor()
            monitor.add_handler(create_pipeline_reconciliation_handler(repo_path))
            monitor.start()
            logger.info("Container monitor started for runtime liveness checks")
        except Exception as monitor_err:
            logger.warning(
                "Container monitor startup failed",
                error=str(monitor_err),
            )

        # --- Health check framework initialization ---
        # Register all Tier 1 (programmatic) health checks and run the
        # STARTUP trigger against every RUNNING pipeline.  The runner is
        # stored on app.config so the on-demand endpoint (routes/health.py)
        # and phase-advance gating (routes/phases.py) can access it.
        # See orchestrator/health_checks/README.md for the full framework.
        try:
            from health_checks.runner import HealthCheckRunner
            from health_checks.tier1 import (
                ContainerLivenessCheck,
                PhaseOutputPresenceCheck,
                StartupStateCheck,
                StateConsistencyCheck,
            )

            runner = HealthCheckRunner()
            runner.register(ContainerLivenessCheck())  # Docker containers alive?
            runner.register(StartupStateCheck())  # Post-reconciliation clean?
            runner.register(PhaseOutputPresenceCheck())  # Agents produced artifacts?
            runner.register(StateConsistencyCheck())  # State vs Docker vs contract?

            # Store runner on app for access by routes and other modules
            app.config["HEALTH_CHECK_RUNNER"] = runner

            # Wire runner into container monitor so RUNTIME_TICK checks fire
            # automatically when container state changes are detected.
            try:
                monitor = get_container_monitor()
                monitor.set_health_check_runner(runner, repo_path)
            except Exception:
                pass  # Monitor may not be available; health checks still work on-demand

            # Run STARTUP health checks on all RUNNING pipelines to catch
            # any inconsistencies left over from a previous crash/restart.
            from health_checks.context import PipelineHealthContext
            from health_checks.types import HealthTrigger
            from state_store import get_state_store as _get_store

            startup_store = _get_store(repo_path)
            for pid in startup_store.list_pipelines():
                try:
                    pipeline = startup_store.load_pipeline(pid)
                    if pipeline.status.value == "running":
                        try:
                            from docker_client import get_docker_client as _get_dc

                            dc = _get_dc()
                        except Exception:
                            dc = None
                        ctx = PipelineHealthContext(
                            pipeline=pipeline,
                            repo_path=Path(repo_path),
                            trigger=HealthTrigger.STARTUP.value,
                            docker_client=dc,
                            state_store=startup_store,
                        )
                        results = runner.run(ctx, HealthTrigger.STARTUP)
                        if results:
                            logger.info(
                                "Startup health check completed",
                                pipeline_id=pid,
                                result_count=len(results),
                            )
                except Exception as hc_err:
                    # Per-pipeline errors are non-fatal; log and continue
                    logger.debug(
                        "Startup health check failed for pipeline",
                        pipeline_id=pid,
                        error=str(hc_err),
                    )

            logger.info("Health check framework initialized")
        except Exception as hc_init_err:
            # Framework init failure is non-fatal — the orchestrator still
            # operates, but health checks are unavailable (503 on the endpoint).
            logger.warning(
                "Health check framework initialization failed",
                error=str(hc_init_err),
            )

    if debug:
        # Use Flask's built-in server for development
        app.run(host=host, port=port, debug=True)
    else:
        # Use waitress for production
        from waitress import serve

        serve(app, host=host, port=port, threads=16)

    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Check orchestrator health."""
    import urllib.error
    import urllib.request

    host = args.host or "localhost"
    port = args.port or ORCHESTRATOR_PORT
    url = f"http://{host}:{port}/api/v1/health"

    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode())

            print(f"Status: {data.get('status', 'unknown')}")
            print(f"Version: {data.get('version', 'unknown')}")

            if data.get("status") == "healthy":
                print("Orchestrator is healthy")
                return 0
            else:
                print("Orchestrator is not healthy")
                return 1

    except urllib.error.URLError as e:
        print(f"Failed to connect to orchestrator: {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Health check failed: {e}", file=sys.stderr)
        return 1


def cmd_pipelines_list(args: argparse.Namespace) -> int:
    """List all pipelines."""
    from state_store import get_state_store

    repo_path = Path(args.repo_path) if args.repo_path else Path.cwd()
    store = get_state_store(repo_path)

    pipeline_ids = store.list_pipelines()

    if not pipeline_ids:
        print("No pipelines found")
        return 0

    if args.json:
        pipelines = []
        for pid in pipeline_ids:
            pipeline = store.load_pipeline(pid)
            pipelines.append(pipeline.model_dump(mode="json"))
        print(json.dumps(pipelines, indent=2, default=str))
    else:
        print(f"Found {len(pipeline_ids)} pipeline(s):\n")
        for pid in pipeline_ids:
            try:
                pipeline = store.load_pipeline(pid)
                print(f"  {pid}")
                print(f"    Status: {pipeline.status.value}")
                print(f"    Issue: #{pipeline.issue_number}")
                print(
                    f"    Phase: {pipeline.current_phase.value if pipeline.current_phase else 'none'}"
                )
                print()
            except Exception as e:
                print(f"  {pid} (error loading: {e})")

    return 0


def cmd_pipelines_create(args: argparse.Namespace) -> int:
    """Create a new pipeline."""
    from state_store import get_state_store

    repo_path = Path(args.repo_path) if args.repo_path else Path.cwd()
    store = get_state_store(repo_path)

    try:
        pipeline = store.create_pipeline(
            issue_number=args.issue,
            repo=args.repo,
            branch=args.branch or f"egg/issue-{args.issue}",
        )

        if args.json:
            print(json.dumps(pipeline.model_dump(mode="json"), indent=2, default=str))
        else:
            print(f"Created pipeline: {pipeline.id}")
            print(f"  Issue: #{pipeline.issue_number}")
            print(f"  Repo: {pipeline.repo}")
            print(f"  Branch: {pipeline.branch}")
            print(f"  Status: {pipeline.status.value}")

        return 0

    except Exception as e:
        print(f"Failed to create pipeline: {e}", file=sys.stderr)
        return 1


def cmd_pipelines_status(args: argparse.Namespace) -> int:
    """Get pipeline status."""
    from state_store import PipelineNotFoundError, get_state_store

    repo_path = Path(args.repo_path) if args.repo_path else Path.cwd()
    store = get_state_store(repo_path)

    try:
        pipeline = store.load_pipeline(args.pipeline_id)

        if args.json:
            print(json.dumps(pipeline.model_dump(mode="json"), indent=2, default=str))
        else:
            print(f"Pipeline: {pipeline.id}")
            print(f"  Issue: #{pipeline.issue_number}")
            print(f"  Repo: {pipeline.repo}")
            print(f"  Branch: {pipeline.branch}")
            print(f"  Status: {pipeline.status.value}")
            print(f"  Phase: {pipeline.current_phase.value if pipeline.current_phase else 'none'}")
            print(f"  Created: {pipeline.created_at}")
            print(f"  Updated: {pipeline.updated_at}")

            # Show decisions if any
            pending = pipeline.get_pending_decisions()
            if pending:
                print(f"\n  Pending Decisions ({len(pending)}):")
                for decision in pending:
                    print(f"    [{decision.id}] {decision.question}")

        return 0

    except PipelineNotFoundError:
        print(f"Pipeline not found: {args.pipeline_id}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Failed to get pipeline status: {e}", file=sys.stderr)
        return 1


def cmd_pipelines_delete(args: argparse.Namespace) -> int:
    """Delete a pipeline."""
    from state_store import PipelineNotFoundError, get_state_store

    repo_path = Path(args.repo_path) if args.repo_path else Path.cwd()
    store = get_state_store(repo_path)

    try:
        store.delete_pipeline(args.pipeline_id)
        print(f"Deleted pipeline: {args.pipeline_id}")
        return 0

    except PipelineNotFoundError:
        print(f"Pipeline not found: {args.pipeline_id}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Failed to delete pipeline: {e}", file=sys.stderr)
        return 1


def cmd_gateway_status(args: argparse.Namespace) -> int:
    """Check gateway connection status."""
    from gateway_client import get_gateway_client

    client = get_gateway_client()
    health = client.check_health()

    if args.json:
        print(
            json.dumps(
                {
                    "healthy": health.healthy,
                    "status": health.status,
                    "version": health.version,
                    "uptime_seconds": health.uptime_seconds,
                    "error": health.error,
                },
                indent=2,
            )
        )
    else:
        print(f"Gateway Status: {health.status}")
        if health.version:
            print(f"  Version: {health.version}")
        if health.uptime_seconds:
            print(f"  Uptime: {health.uptime_seconds:.0f}s")
        if health.error:
            print(f"  Error: {health.error}")

    return 0 if health.healthy else 1


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="egg-orchestrator",
        description="SDLC Pipeline Orchestrator",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve command
    serve_parser = subparsers.add_parser("serve", help="Start the API server")
    serve_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=ORCHESTRATOR_PORT,
        help=f"Port to listen on (default: {ORCHESTRATOR_PORT})",
    )
    serve_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode",
    )
    serve_parser.set_defaults(func=cmd_serve)

    # health command
    health_parser = subparsers.add_parser("health", help="Check service health")
    health_parser.add_argument(
        "--host",
        help="Orchestrator host to check",
    )
    health_parser.add_argument(
        "--port",
        type=int,
        help="Orchestrator port to check",
    )
    health_parser.set_defaults(func=cmd_health)

    # gateway command
    gateway_parser = subparsers.add_parser("gateway", help="Gateway operations")
    gateway_subparsers = gateway_parser.add_subparsers(dest="gateway_command")

    gateway_status_parser = gateway_subparsers.add_parser(
        "status",
        help="Check gateway connection status",
    )
    gateway_status_parser.add_argument("--json", action="store_true", help="Output as JSON")
    gateway_status_parser.set_defaults(func=cmd_gateway_status)

    # pipelines command group
    pipelines_parser = subparsers.add_parser("pipelines", help="Pipeline operations")
    pipelines_subparsers = pipelines_parser.add_subparsers(dest="pipelines_command")

    # pipelines list
    list_parser = pipelines_subparsers.add_parser("list", help="List pipelines")
    list_parser.add_argument("--repo-path", help="Repository path")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.set_defaults(func=cmd_pipelines_list)

    # pipelines create
    create_parser = pipelines_subparsers.add_parser("create", help="Create a pipeline")
    create_parser.add_argument("--issue", type=int, required=True, help="Issue number")
    create_parser.add_argument("--repo", required=True, help="Repository (owner/repo)")
    create_parser.add_argument("--branch", help="Branch name (default: egg/issue-N)")
    create_parser.add_argument("--repo-path", help="Repository path")
    create_parser.add_argument("--json", action="store_true", help="Output as JSON")
    create_parser.set_defaults(func=cmd_pipelines_create)

    # pipelines status
    status_parser = pipelines_subparsers.add_parser("status", help="Get pipeline status")
    status_parser.add_argument("pipeline_id", help="Pipeline ID")
    status_parser.add_argument("--repo-path", help="Repository path")
    status_parser.add_argument("--json", action="store_true", help="Output as JSON")
    status_parser.set_defaults(func=cmd_pipelines_status)

    # pipelines delete
    delete_parser = pipelines_subparsers.add_parser("delete", help="Delete a pipeline")
    delete_parser.add_argument("pipeline_id", help="Pipeline ID")
    delete_parser.add_argument("--repo-path", help="Repository path")
    delete_parser.set_defaults(func=cmd_pipelines_delete)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    # Handle subcommand groups
    if args.command == "pipelines" and not args.pipelines_command:
        parser.parse_args(["pipelines", "--help"])
        return 1

    if args.command == "gateway" and not args.gateway_command:
        parser.parse_args(["gateway", "--help"])
        return 1

    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
