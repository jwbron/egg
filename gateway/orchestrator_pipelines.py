"""HTTP client for the orchestrator's pipeline listing.

Worktree cleanup must not equate "no live container" with "orphaned
worktree": a pipeline parked at a HITL gate (or between phases) has no
running containers and no sessions, yet its worktree holds the contract
and any un-pushed work. In #3070 a redeploy ran startup cleanup with
``active_containers=0`` and force-removed every worktree — including a
pipeline whose refine analysis had just been operator-approved.

``fetch_active_pipeline_ids`` asks the orchestrator which pipelines are
non-terminal so cleanup can preserve their worktrees regardless of
container liveness. Failure returns ``None`` (never an empty set) so
callers can distinguish "verified nothing active" from "could not
verify" and fail safe by skipping deletion entirely.
"""

from __future__ import annotations

import json
import logging
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover - fall back to stdlib logger

    def get_logger(  # type: ignore[misc]
        name: str,
        level: int | str = logging.INFO,
        component: str | None = None,
    ) -> logging.Logger:
        return logging.getLogger(name)


logger = get_logger("gateway.orchestrator_pipelines")

_DEFAULT_ORCHESTRATOR_URL = "http://egg-orchestrator:9849"
_DEFAULT_FETCH_TIMEOUT_SECONDS = 15
# Startup cleanup runs in a background thread, so a long wait is cheap;
# a redeploy restarts both pods and the orchestrator's cold boot
# (image pull + startup reconciliation) can take minutes.
_DEFAULT_MAX_WAIT_SECONDS = 600
_DEFAULT_POLL_INTERVAL_SECONDS = 5.0


def _orchestrator_url() -> str:
    return os.environ.get("EGG_ORCHESTRATOR_URL", _DEFAULT_ORCHESTRATOR_URL).rstrip("/")


def fetch_active_pipeline_ids(
    *,
    timeout: float = _DEFAULT_FETCH_TIMEOUT_SECONDS,
) -> set[str] | None:
    """Return the IDs of all non-terminal pipelines, or ``None`` on failure.

    Queries ``GET /api/v1/pipelines?active_only=true``. ``None`` (as
    opposed to an empty set) means the answer is unknown — the caller
    must not treat it as "no active pipelines".
    """
    url = f"{_orchestrator_url()}/api/v1/pipelines?active_only=true"
    req = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        logger.warning(
            "Could not fetch active pipelines from orchestrator",
            url=url,
            error=str(exc),
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Unexpected error fetching active pipelines",
            url=url,
            error=str(exc),
        )
        return None

    try:
        parsed = json.loads(body)
        pipelines = parsed["data"]["pipelines"]
        ids = {p["id"] for p in pipelines if p.get("id")}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "Malformed active-pipelines response from orchestrator",
            url=url,
            error=str(exc),
        )
        return None

    logger.info("Fetched active pipelines from orchestrator", count=len(ids))
    return ids


def wait_for_active_pipeline_ids(
    *,
    max_wait_seconds: float | None = None,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> set[str] | None:
    """Poll the orchestrator until it answers or the deadline passes.

    Intended for gateway startup, where the orchestrator pod may still be
    booting (on a redeploy both restart together). Returns the active
    pipeline-ID set on success, ``None`` once ``max_wait_seconds`` is
    exhausted. ``EGG_CLEANUP_ORCHESTRATOR_WAIT_SECONDS`` overrides the
    default deadline; ``0`` disables waiting (single attempt).
    """
    if max_wait_seconds is None:
        try:
            max_wait_seconds = float(
                os.environ.get(
                    "EGG_CLEANUP_ORCHESTRATOR_WAIT_SECONDS",
                    str(_DEFAULT_MAX_WAIT_SECONDS),
                )
            )
        except ValueError:
            max_wait_seconds = _DEFAULT_MAX_WAIT_SECONDS

    deadline = time.monotonic() + max_wait_seconds
    attempt = 0
    while True:
        attempt += 1
        ids = fetch_active_pipeline_ids()
        if ids is not None:
            return ids
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.error(
                "Orchestrator did not answer active-pipelines query before "
                "deadline; worktree cleanup cannot verify pipeline liveness",
                attempts=attempt,
                max_wait_seconds=max_wait_seconds,
            )
            return None
        time.sleep(min(poll_interval_seconds, remaining))
