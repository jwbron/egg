"""Optional tracemalloc-based memory sampler for gateway diagnostics.

Enable by setting ``GATEWAY_MEM_TRACE=1`` in the environment. The sampler runs
on a background daemon thread and emits one structured log record per interval
(default 30s) containing the current RSS and the top-N allocation sites.

Output goes through the egg-logging logger (stdout), so traces survive a pod
OOM via ``kubectl logs --previous -n egg-system <gateway-pod>``. We deliberately
do **not** write to disk: the ``/home/egg/.egg-state`` mount is an ``emptyDir``
in ``k8s/base/gateway-deployment.yaml`` and is wiped on pod restart, so file
persistence there wouldn't survive the OOM we're trying to diagnose.

This is opt-in because ``tracemalloc`` adds per-allocation overhead (captures a
25-frame stack on every allocation). Enable only when actively investigating a
memory issue. See issue #1885.

Tuning env vars:
    GATEWAY_MEM_TRACE=1                      — turn on sampling
    GATEWAY_MEM_TRACE_INTERVAL_SECONDS=30    — seconds between samples
    GATEWAY_MEM_TRACE_TOP_N=15               — number of top allocation sites
"""

from __future__ import annotations

import os
import threading
import time
import tracemalloc
from typing import Any

from egg_logging import get_logger

logger = get_logger("gateway.mem_trace")


ENABLE_ENV_VAR = "GATEWAY_MEM_TRACE"
INTERVAL_ENV_VAR = "GATEWAY_MEM_TRACE_INTERVAL_SECONDS"
TOP_N_ENV_VAR = "GATEWAY_MEM_TRACE_TOP_N"

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_TOP_N = 15
FRAME_DEPTH = 25


def _read_rss_mb() -> float | None:
    """Return the process RSS in MB from /proc/self/status, or None on failure."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024
    except OSError, ValueError:
        return None
    return None


def _sample_once(top_n: int) -> dict[str, Any]:
    """Take one tracemalloc snapshot and return a log-friendly record."""
    snap = tracemalloc.take_snapshot()
    stats = snap.statistics("lineno")[:top_n]
    return {
        "rss_mb": _read_rss_mb(),
        "top": [
            {
                "loc": str(s.traceback[-1]) if s.traceback else "<unknown>",
                "size_kb": s.size // 1024,
                "count": s.count,
            }
            for s in stats
        ],
    }


def _sampler_loop(interval_seconds: float, top_n: int) -> None:
    while True:
        time.sleep(interval_seconds)
        try:
            record = _sample_once(top_n)
            logger.info("gateway_mem_trace", **record)
        except Exception as exc:
            # Never let the sampler crash the gateway; just log and keep going.
            logger.warning("gateway_mem_trace_failed", error=str(exc))


_started = False


def _is_truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def start_if_enabled() -> bool:
    """Start the sampler iff ``GATEWAY_MEM_TRACE`` is truthy.

    Returns True if sampling was started, False otherwise. Safe to call multiple
    times; subsequent calls after the first are no-ops.
    """
    global _started
    if _started:
        return False

    if not _is_truthy(os.environ.get(ENABLE_ENV_VAR, "")):
        return False

    try:
        interval = float(os.environ.get(INTERVAL_ENV_VAR, DEFAULT_INTERVAL_SECONDS))
    except ValueError:
        interval = DEFAULT_INTERVAL_SECONDS
    interval = max(1.0, interval)

    try:
        top_n = int(os.environ.get(TOP_N_ENV_VAR, DEFAULT_TOP_N))
    except ValueError:
        top_n = DEFAULT_TOP_N
    top_n = max(1, top_n)

    if not tracemalloc.is_tracing():
        tracemalloc.start(FRAME_DEPTH)

    thread = threading.Thread(
        target=_sampler_loop,
        args=(interval, top_n),
        name="gateway-mem-trace",
        daemon=True,
    )
    thread.start()
    _started = True
    logger.info(
        "Memory trace sampler started",
        interval_seconds=interval,
        top_n=top_n,
        frame_depth=FRAME_DEPTH,
    )
    return True
