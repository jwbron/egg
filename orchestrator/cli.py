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

    class _FallbackLogger:
        """Minimal wrapper compatible with EggLogger's structured kwargs API."""

        def __init__(self, logger: logging.Logger):
            self._logger = logger

        @staticmethod
        def _fmt(msg: str, kwargs: dict) -> str:  # type: ignore[type-arg]
            """Append structured kwargs to the message so context isn't lost."""
            extra = " ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
            return f"{msg} {extra}".rstrip() if extra else msg

        def debug(self, msg: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self._logger.debug(self._fmt(msg, kwargs), *args)

        def info(self, msg: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self._logger.info(self._fmt(msg, kwargs), *args)

        def warning(self, msg: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            self._logger.warning(self._fmt(msg, kwargs), *args)

        def error(self, msg: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            exc_info = kwargs.pop("exc_info", None)
            self._logger.error(self._fmt(msg, kwargs), *args, exc_info=exc_info)

        def critical(self, msg: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            exc_info = kwargs.pop("exc_info", None)
            self._logger.critical(self._fmt(msg, kwargs), *args, exc_info=exc_info)

        def exception(self, msg: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            exc_info = kwargs.pop("exc_info", True)
            self._logger.exception(self._fmt(msg, kwargs), *args, exc_info=exc_info)

    def get_logger(name: str, **kwargs) -> _FallbackLogger:  # type: ignore[misc]
        return _FallbackLogger(logging.getLogger(name))

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
        store = None
        try:
            from docker_client import get_docker_client
            from startup_reconciliation import reconcile_stale_containers
            from state_store import get_state_store

            store = get_state_store(repo_path)
            recovered = reconcile_stale_containers(store, get_docker_client())
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

            # Start periodic reconciliation to detect stale containers
            # that may have exited between event-driven checks.
            if store is None:
                from state_store import get_state_store as _get_state_store

                store = _get_state_store(repo_path)
            monitor.start_periodic_reconciliation(store)
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
            from health_checks.tier2 import AgentInspectorCheck

            runner = HealthCheckRunner()
            # Tier 1 (programmatic)
            runner.register(ContainerLivenessCheck())  # Docker containers alive?
            runner.register(StartupStateCheck())  # Post-reconciliation clean?
            runner.register(PhaseOutputPresenceCheck())  # Agents produced artifacts?
            runner.register(StateConsistencyCheck())  # State vs Docker vs contract?
            # Tier 2 (semantic — runs on escalation per DD-6)
            runner.register(AgentInspectorCheck())  # Claude-powered agent analysis

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
        # Use waitress for production.
        # 16 threads handles concurrent requests including Redis XREAD BLOCK
        # long-polling (capped at 60s per request in messages.py). Waitress
        # thread pool accommodates blocking I/O without requiring async workers.
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


def cmd_anchor_init(args: argparse.Namespace) -> int:
    """Initialize an anchor file for this agent."""
    import os
    from datetime import UTC, datetime

    agent_id = os.environ.get("AGENT_ANCHOR_ID")
    if not agent_id:
        print("Error: AGENT_ANCHOR_ID environment variable not set", file=sys.stderr)
        return 1

    pipeline_id = os.environ.get("EGG_PIPELINE_ID", "unknown")
    agent_role = os.environ.get("EGG_AGENT_ROLE", "unknown")

    # Import anchor library
    try:
        from egg_anchor.models import (
            AgentAnchor,
            AnchorMeta,
            AnchorStatus,
            BRCPhase,
            BRCState,
            TaskInfo,
        )
        from egg_anchor.loader import save_anchor, sync_anchor_to_api
    except ImportError:
        print("Error: egg_anchor library not available", file=sys.stderr)
        return 1

    now = datetime.now(UTC)
    anchor = AgentAnchor(
        meta=AnchorMeta(
            schema_version="1.0",
            created_at=now,
            updated_at=now,
            sequence=0,
        ),
        agent_id=agent_id,
        role=agent_role,
        team=[],
        task=TaskInfo(
            id=args.task_id or f"task-{agent_id}",
            description=args.task,
            phase=args.phase,
        ),
        status=AnchorStatus.INITIALIZING,
        pipeline_id=pipeline_id,
        progress=[],
        decisions=[],
        brc_state=BRCState(phase=BRCPhase.ORIENT),
        key_context=[],
        errors_encountered=[],
        files_modified=[],
    )

    path = save_anchor(anchor)
    sync_anchor_to_api(anchor)

    if getattr(args, "json", False):
        print(json.dumps({"success": True, "path": str(path), "agent_id": agent_id}))
    else:
        print(f"Anchor initialized for agent {agent_id} at {path}")

    return 0


def cmd_anchor_update(args: argparse.Namespace) -> int:
    """Update the current agent's anchor."""
    import os
    from datetime import UTC, datetime

    agent_id = os.environ.get("AGENT_ANCHOR_ID")
    if not agent_id:
        print("Error: AGENT_ANCHOR_ID environment variable not set", file=sys.stderr)
        return 1

    try:
        from egg_anchor.models import (
            AnchorStatus,
            Decision,
            ErrorEncountered,
            KeyContext,
            ProgressItem,
            ProgressState,
        )
        from egg_anchor.loader import load_anchor, save_anchor, sync_anchor_to_api
    except ImportError:
        print("Error: egg_anchor library not available", file=sys.stderr)
        return 1

    anchor = load_anchor(agent_id)
    if not anchor:
        print(f"Error: No anchor file found for agent {agent_id}", file=sys.stderr)
        return 1

    now = datetime.now(UTC)
    changed = False

    if args.status:
        try:
            anchor.status = AnchorStatus(args.status)
            changed = True
        except ValueError:
            print(f"Error: Invalid status '{args.status}'", file=sys.stderr)
            return 1

    if args.progress:
        step, state = args.progress
        try:
            item = ProgressItem(step=step, state=ProgressState(state), timestamp=now)
            # Update existing or append
            updated = False
            for i, p in enumerate(anchor.progress):
                if p.step == step:
                    anchor.progress[i] = item
                    updated = True
                    break
            if not updated:
                anchor.progress.append(item)
            changed = True
        except ValueError:
            print(f"Error: Invalid progress state '{state}'", file=sys.stderr)
            return 1

    if args.decision:
        question, answer = args.decision
        import uuid
        decision = Decision(
            id=str(uuid.uuid4())[:8],
            question=question,
            answer=answer,
            timestamp=now,
        )
        anchor.decisions.append(decision)
        changed = True

    if args.key_context:
        label, value = args.key_context
        ctx = KeyContext(label=label, value=value)
        # Update existing or append
        updated = False
        for i, k in enumerate(anchor.key_context):
            if k.label == label:
                anchor.key_context[i] = ctx
                updated = True
                break
        if not updated:
            anchor.key_context.append(ctx)
        changed = True

    if args.error:
        err = ErrorEncountered(error=args.error, timestamp=now)
        anchor.errors_encountered.append(err)
        changed = True

    if args.file:
        if args.file not in anchor.files_modified:
            anchor.files_modified.append(args.file)
            changed = True

    if not changed:
        print("No updates specified", file=sys.stderr)
        return 1

    anchor.meta.updated_at = now
    anchor.meta.sequence += 1

    save_anchor(anchor)
    sync_anchor_to_api(anchor)

    if getattr(args, "json", False):
        print(json.dumps({"success": True, "agent_id": agent_id, "sequence": anchor.meta.sequence}))
    else:
        print(f"Anchor updated for agent {agent_id} (sequence {anchor.meta.sequence})")

    return 0


def cmd_anchor_show(args: argparse.Namespace) -> int:
    """Display an anchor."""
    import os

    try:
        from egg_anchor.loader import load_anchor
    except ImportError:
        print("Error: egg_anchor library not available", file=sys.stderr)
        return 1

    if getattr(args, "team", False):
        # Fetch team anchor from API
        orchestrator_url = os.environ.get(
            "EGG_ORCHESTRATOR_URL", "http://egg-orchestrator:9849"
        )
        pipeline_id = os.environ.get("EGG_PIPELINE_ID", "unknown")
        try:
            import requests
            resp = requests.get(
                f"{orchestrator_url}/api/v1/anchors/team/{pipeline_id}",
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                if getattr(args, "json", False):
                    print(json.dumps(data))
                else:
                    print(json.dumps(data, indent=2))
                return 0
            else:
                print(f"Error: API returned {resp.status_code}", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"Error fetching team anchor: {e}", file=sys.stderr)
            return 1

    agent_id = getattr(args, "agent", None) or os.environ.get("AGENT_ANCHOR_ID")
    if not agent_id:
        print("Error: No agent ID specified and AGENT_ANCHOR_ID not set", file=sys.stderr)
        return 1

    # Try local file first
    anchor = load_anchor(agent_id)
    if anchor:
        data = anchor.to_dict()
        if getattr(args, "json", False):
            print(json.dumps(data))
        else:
            print(json.dumps(data, indent=2))
        return 0

    # Try API for cross-agent reads
    if getattr(args, "agent", None):
        orchestrator_url = os.environ.get(
            "EGG_ORCHESTRATOR_URL", "http://egg-orchestrator:9849"
        )
        try:
            import requests
            resp = requests.get(
                f"{orchestrator_url}/api/v1/anchors/{agent_id}",
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                if getattr(args, "json", False):
                    print(json.dumps(data))
                else:
                    print(json.dumps(data, indent=2))
                return 0
        except Exception:
            pass

    print(f"No anchor found for agent {agent_id}", file=sys.stderr)
    return 1


def cmd_anchor_validate(args: argparse.Namespace) -> int:
    """Validate the current agent's anchor."""
    import os

    agent_id = os.environ.get("AGENT_ANCHOR_ID")
    if not agent_id:
        print("Error: AGENT_ANCHOR_ID environment variable not set", file=sys.stderr)
        return 1

    try:
        from egg_anchor.loader import load_anchor
        from egg_anchor.validator import validate_anchor, check_size_budget
    except ImportError:
        print("Error: egg_anchor library not available", file=sys.stderr)
        return 1

    anchor = load_anchor(agent_id)
    if not anchor:
        print(f"Error: No anchor file found for agent {agent_id}", file=sys.stderr)
        return 1

    schema_errors = validate_anchor(anchor)
    budget = check_size_budget(anchor)

    valid = len(schema_errors) == 0 and budget.within_budget

    if getattr(args, "json", False):
        result = {
            "valid": valid,
            "agent_id": agent_id,
            "schema_errors": schema_errors,
            "size_bytes": budget.size_bytes,
            "within_budget": budget.within_budget,
            "warnings": budget.warnings,
            "budget_errors": budget.errors,
        }
        print(json.dumps(result))
    else:
        if schema_errors:
            print("Schema validation errors:")
            for err in schema_errors:
                print(f"  - {err}")
        if budget.warnings:
            for w in budget.warnings:
                print(f"Warning: {w}")
        if budget.errors:
            for e in budget.errors:
                print(f"Error: {e}")
        if valid:
            print(f"Anchor for {agent_id} is valid ({budget.size_bytes} bytes)")

    return 0 if valid else 1


def cmd_anchor_cleanup(args: argparse.Namespace) -> int:
    """Remove orphaned anchor files."""
    import os
    from pathlib import Path

    repo_path = os.environ.get("EGG_REPO_PATH", ".")
    anchor_dir = Path(repo_path) / ".egg-state" / "agent-anchors"

    if not anchor_dir.exists():
        if getattr(args, "json", False):
            print(json.dumps({"success": True, "removed": 0}))
        else:
            print("No anchor directory found")
        return 0

    removed = 0
    for anchor_file in anchor_dir.glob("*.json"):
        try:
            anchor_file.unlink()
            removed += 1
        except OSError as e:
            print(f"Failed to remove {anchor_file}: {e}", file=sys.stderr)

    if getattr(args, "json", False):
        print(json.dumps({"success": True, "removed": removed}))
    else:
        print(f"Removed {removed} anchor file(s)")

    return 0


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

    # anchor command group
    anchor_parser = subparsers.add_parser("anchor", help="Agent anchor operations")
    anchor_subparsers = anchor_parser.add_subparsers(dest="anchor_command")

    # anchor init
    anchor_init_parser = anchor_subparsers.add_parser(
        "init", help="Initialize an anchor file for this agent"
    )
    anchor_init_parser.add_argument("--task", required=True, help="Task description")
    anchor_init_parser.add_argument("--task-id", default="", help="Task ID")
    anchor_init_parser.add_argument("--phase", default="implement", help="Pipeline phase")
    anchor_init_parser.add_argument("--json", action="store_true", help="Output as JSON")
    anchor_init_parser.set_defaults(func=cmd_anchor_init)

    # anchor update
    anchor_update_parser = anchor_subparsers.add_parser(
        "update", help="Update the current agent's anchor"
    )
    anchor_update_parser.add_argument("--status", help="New status")
    anchor_update_parser.add_argument(
        "--progress", nargs=2, metavar=("STEP", "STATE"),
        help="Add/update a progress item (step name and state)"
    )
    anchor_update_parser.add_argument("--decision", nargs=2, metavar=("QUESTION", "ANSWER"), help="Add a decision")
    anchor_update_parser.add_argument(
        "--key-context", nargs=2, metavar=("LABEL", "VALUE"), help="Add a key context item"
    )
    anchor_update_parser.add_argument("--error", help="Record an error encountered")
    anchor_update_parser.add_argument("--file", help="Add a modified file path")
    anchor_update_parser.add_argument("--json", action="store_true", help="Output as JSON")
    anchor_update_parser.set_defaults(func=cmd_anchor_update)

    # anchor show
    anchor_show_parser = anchor_subparsers.add_parser(
        "show", help="Display an anchor"
    )
    anchor_show_parser.add_argument("--agent", help="Agent ID to show (default: own)")
    anchor_show_parser.add_argument("--team", action="store_true", help="Show team anchor")
    anchor_show_parser.add_argument("--json", action="store_true", help="Output as JSON")
    anchor_show_parser.set_defaults(func=cmd_anchor_show)

    # anchor validate
    anchor_validate_parser = anchor_subparsers.add_parser(
        "validate", help="Validate the current agent's anchor"
    )
    anchor_validate_parser.add_argument("--json", action="store_true", help="Output as JSON")
    anchor_validate_parser.set_defaults(func=cmd_anchor_validate)

    # anchor cleanup
    anchor_cleanup_parser = anchor_subparsers.add_parser(
        "cleanup", help="Remove orphaned anchor files"
    )
    anchor_cleanup_parser.add_argument("--json", action="store_true", help="Output as JSON")
    anchor_cleanup_parser.set_defaults(func=cmd_anchor_cleanup)

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

    if args.command == "anchor" and not args.anchor_command:
        parser.parse_args(["anchor", "--help"])
        return 1

    if hasattr(args, "func"):
        return args.func(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
