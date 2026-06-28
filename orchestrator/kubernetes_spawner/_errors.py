"""Spawn-failure classification + k8s name fitting (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import hashlib

from kubernetes_spawner import (
    _PERMANENT_MESSAGE_FRAGMENTS,
    _PERMANENT_STATUS_CODES,
    _TRANSIENT_STATUS_CODES,
)


def _fit_k8s_name(name: str, maxlen: int = 63) -> str:
    """Fit an (unprefixed) k8s name to ``maxlen`` chars, RFC-1123-safe.

    Mirrors ``KubernetesClient._normalize_k8s_job_name``'s truncation shape —
    ``readable[:maxlen-9] + '-' + 8-char sha1`` — so a long
    ``egg-agent-<pipeline>-<slice>-<role>-<event>`` one-shot name stays within
    the 63-char budget while preserving the ``egg-agent-`` prefix. Idempotent
    for already-short names.
    """
    if len(name) <= maxlen:
        return name
    digest = hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{name[: maxlen - 9].rstrip('-')}-{digest}"


def _is_transient_spawn_failure(e: BaseException) -> bool:
    """Classify whether a worktree-creation failure should be retried.

    Coarse classifier based on ``GatewayError.status_code`` and message
    content. Refinement via ``GatewayError.details["errors"]`` is blocked
    on #1838.

    Rules (in priority order):
    1. Message contains a permanent fragment (e.g. "Repository not found"): permanent.
    2. ``status_code`` is a known permanent code (400/401/403/404/422): permanent.
    3. ``status_code`` is a known transient code (408/429/5xx): transient.
    4. ``status_code`` is any other HTTP status: permanent (fail fast).
    5. No ``status_code`` (connection-level failure or non-HTTP exception):
       transient by default — matches the issue #1839 recommendation of
       "retry by default but bounded."
    """
    message = str(e).lower()
    if any(frag in message for frag in _PERMANENT_MESSAGE_FRAGMENTS):
        return False
    status_code = getattr(e, "status_code", None)
    if status_code is None:
        return True
    if status_code in _PERMANENT_STATUS_CODES:
        return False
    if status_code in _TRANSIENT_STATUS_CODES:
        return True
    return False


def _classify_spawn_error(e: BaseException | None) -> str | None:
    """Short tag used in structured spawn-attempt logs.

    Priority order matches ``_is_transient_spawn_failure`` so the logged
    category always agrees with the actual retry decision.
    """
    if e is None:
        return None
    message = str(e).lower()
    if any(frag in message for frag in _PERMANENT_MESSAGE_FRAGMENTS):
        return "permanent_message"
    status_code = getattr(e, "status_code", None)
    if status_code in _PERMANENT_STATUS_CODES:
        return f"permanent_{status_code}"
    if status_code in _TRANSIENT_STATUS_CODES:
        return f"transient_{status_code}"
    if status_code is None:
        return type(e).__name__
    return f"unknown_{status_code}"
