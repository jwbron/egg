"""Deployment introspection and action endpoints.

Exposes read-only introspection tools
(``get_deployment_context``, ``validate_deployment_manifests``,
``get_service_logs``) and mutating actions (``prune_stale_worktrees`` —
proxied to the gateway, ``validate_network_isolation``,
``rebuild_and_rollout``) for operator diagnostics on the Kubernetes
runtime.

Every route requires the lifecycle bearer token (parity with the
fix made for #1769) because these are agent-visible via the MCP
server. Runtime-specific routes degrade gracefully on Docker by
returning a structured ``not_available_on_runtime`` payload.

This package is the stable public API surface for the ``deployment``
blueprint (file-decomposition pattern, #3312). Per decision-8 the
``@deployment_bp.route`` decorators stay here on thin wrappers that
delegate to private submodules:

- ``_runtime``             — runtime detection + degrade-gracefully payloads
- ``_cluster_detection``   — k3s / CNI / image-tag apiserver probes
- ``_context``             — ``get_deployment_context``
- ``_service_logs``        — ``get_service_logs``
- ``_manifest_validation`` — ``validate_deployment_manifests`` + warning rules
- ``_prune``               — ``prune_worktrees_proxy`` (gateway proxy)
- ``_network_probe``       — ``validate_network_isolation`` + probe lifecycle
- ``_rebuild``             — ``rebuild_and_rollout`` + progress-stream plumbing

The barrel re-exports every externally-referenced or test-patched symbol so
``from routes.deployment import _foo`` and ``patch("routes.deployment._foo")``
keep resolving. Submodules invoke the barrel-patched dependencies through this
package module (``import routes.deployment as _pkg``) so the existing
``patch("routes.deployment.<name>")`` seams stay effective unchanged.

The mutable rollout/stream state (``_REBUILD_IN_PROGRESS``,
``_REBUILD_ACTIVE_STREAM_ID``, the ``_STREAM_*`` buffers/locks) lives HERE,
on the package module: it was a set of module globals of the pre-split file
that both the tests (``dep_mod.X = ...``) and ``_rebuild`` rebind, so keeping
it on the barrel preserves the single canonical binding. ``_rebuild`` reaches
it via ``_pkg`` so reads/writes/mutations all hit the same objects.
"""

from __future__ import annotations

import subprocess  # noqa: F401 — re-exported: tests monkeypatch routes.deployment.subprocess.run
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from flask import Blueprint, Response

# Add parent directory (orchestrator/) to path for imports. The sub-package
# lives one level deeper than the original module, so the walk-up gains a
# ``.parent`` versus the pre-split file.
_parent_path = Path(__file__).parent.parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path for logging (egg-root/shared).
_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


from lifecycle_auth import require_lifecycle_secret  # noqa: E402

logger = get_logger("orchestrator.deployment")

deployment_bp = Blueprint("deployment", __name__, url_prefix="/api/v1/deployment")


# ---------------------------------------------------------------------------
# Canonical rollout / progress-stream state (single source of truth).
#
# These were module globals of the pre-split file. They are rebound by both
# the route tests and ``_rebuild``, so they MUST live on a single module:
# this package. ``_rebuild`` reads/writes/mutates them via ``_pkg``.
# ---------------------------------------------------------------------------

# Single in-process guard. The rebuild writes images to the node's
# containerd cache; running two at once is never what the caller
# wanted.
_REBUILD_LOCK = threading.Lock()
_REBUILD_IN_PROGRESS = False
_REBUILD_ACTIVE_STREAM_ID: str | None = None

# Per-stream buffer.  Each stream is a deque of events that the consumer
# reads via GET /api/v1/deployment/rebuild-and-rollout/streams/<id>.
# Unbounded within a single stream — the retention reaper caps total
# stream count and streams are short-lived (bounded by
# _REDEPLOY_SUBPROCESS_TIMEOUT_SEC).  A maxlen here would silently
# evict old events while the cursor (next_since) keeps incrementing,
# causing positional slices to return empty batches.
_STREAM_BUFFERS: dict[str, deque[dict[str, Any]]] = {}
_STREAM_TERMINATION_TS: dict[str, float] = {}
_STREAM_LOCK = threading.Lock()
_STREAM_TERMINATED: set[str] = set()

# How many finished streams we keep around so a wait=true consumer can
# still fetch the terminal events after the worker exited.  Older
# entries are evicted FIFO — _STREAM_BUFFERS must not grow unbounded
# across a long-running orchestrator (see #1759 review MEDIUM-3).
_STREAM_RETENTION = 16

# Hard timeout for the redeploy subprocess.  If ``make redeploy`` hangs
# (stuck docker build, hung k3s ctr import, network wedge) the
# watchdog kills it, emits a structured ``phase: "timeout"`` event, and
# clears _REBUILD_IN_PROGRESS so the next caller is not wedged at 409
# forever.  30 min is a generous upper bound for a clean rebuild.
_REDEPLOY_SUBPROCESS_TIMEOUT_SEC = 1800


# Private submodules. Imported after the blueprint + shared deps + logger +
# canonical state exist so the ``import routes.deployment as _pkg`` barrel
# access inside them resolves against a populated package module.
from . import (  # noqa: E402,F401
    _cluster_detection,
    _context,
    _manifest_validation,
    _network_probe,
    _prune,
    _rebuild,
    _runtime,
    _service_logs,
)

# Re-export the stable public / test-patched surface (pattern §a/§d).
from ._cluster_detection import (  # noqa: E402,F401
    _NETWORK_POLICY_CNIS,
    _collect_egg_image_tags,
    _detect_cni,
    _detect_k3s,
)
from ._context import _build_deployment_context_payload  # noqa: E402,F401
from ._manifest_validation import (  # noqa: E402,F401
    _DEFAULT_OVERLAY,
    _deployment_containers,
    _deployment_volumes,
    _run_kustomize,
    _validate_deployment_docs,
    _warn,
)
from ._network_probe import (  # noqa: E402,F401
    _K8S_LABEL_VALUE_RE,
    PROBE_COMMAND_TEMPLATE,
    _build_probe_env,
    _build_probe_job_manifest,
    _delete_probe_job,
    _parse_probe_output,
    _read_probe_log,
    _submit_probe_job,
    _wait_for_probe_pod,
)
from ._rebuild import (  # noqa: E402,F401
    _reap_stale_streams_locked,
    _run_redeploy_subprocess,
    _stream_append,
    _stream_is_done,
    _stream_mark_done,
    _stream_snapshot,
)
from ._runtime import (  # noqa: E402,F401
    _current_runtime,
    _not_available_on_runtime,
    _probe_kubernetes_reachable,
    _resolve_runtime,
    _runtime_detection_failed,
)
from ._service_logs import _MAX_LOG_LINES, _SERVICE_LOG_ALLOWLIST  # noqa: E402,F401

# ---- Route registrations -------------------------------------------------
# Decision-8: decorators stay in __init__.py on thin wrappers; the bodies
# live in the private submodules above.


@deployment_bp.route("/context", methods=["GET"])
@require_lifecycle_secret
def get_deployment_context() -> tuple[Response, int]:
    """Return runtime / cluster / image introspection."""
    return _context.get_deployment_context()


@deployment_bp.route("/logs", methods=["GET"])
@require_lifecycle_secret
def get_service_logs() -> tuple[Response, int]:
    """Return logs from the pod(s) backing the gateway or orchestrator Deployment."""
    return _service_logs.get_service_logs()


@deployment_bp.route("/validate-manifests", methods=["POST"])
@require_lifecycle_secret
def validate_deployment_manifests() -> tuple[Response, int]:
    """Static validation of the committed kustomize overlay."""
    return _manifest_validation.validate_deployment_manifests()


@deployment_bp.route("/prune-worktrees", methods=["POST"])
@require_lifecycle_secret
def prune_worktrees_proxy() -> tuple[Response, int]:
    """Proxy to the gateway's worktree-prune endpoint."""
    return _prune.prune_worktrees_proxy()


@deployment_bp.route("/validate-network-isolation", methods=["POST"])
@require_lifecycle_secret
def validate_network_isolation() -> tuple[Response, int]:
    """Spawn a throwaway probe Job and report isolation results."""
    return _network_probe.validate_network_isolation()


@deployment_bp.route("/rebuild-and-rollout", methods=["POST"])
@require_lifecycle_secret
def rebuild_and_rollout() -> tuple[Response, int]:
    """Kick off ``make redeploy`` asynchronously and return a stream handle."""
    return _rebuild.rebuild_and_rollout()


@deployment_bp.route("/rebuild-and-rollout/streams/<stream_id>", methods=["GET"])
@require_lifecycle_secret
def rebuild_stream_read(stream_id: str) -> tuple[Response, int]:
    """Return buffered progress events for *stream_id*."""
    return _rebuild.rebuild_stream_read(stream_id)


__all__ = [
    "deployment_bp",
    "PROBE_COMMAND_TEMPLATE",
    "_build_probe_env",
    "_build_probe_job_manifest",
    "_validate_deployment_docs",
    "_build_deployment_context_payload",
    "_run_redeploy_subprocess",
    "_stream_snapshot",
    "_stream_append",
    "_stream_mark_done",
    "_STREAM_BUFFERS",
    "_STREAM_TERMINATED",
    "_SERVICE_LOG_ALLOWLIST",
    "_MAX_LOG_LINES",
]
