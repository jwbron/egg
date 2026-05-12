"""
Apply-phase Won't-Do drain (issue #1557 task-2-7).

After the APPLIER produces a per-pipeline handoff JSON at
``.egg-state/agent-outputs/<pipeline>-wontdo.json`` and its
CONSENSUS_PROPOSE → REVIEWER_CONTRACT ACK cycle confirms, the
orchestrator drains the handoff by iterating the entries and
calling the orchestrator-only gateway route
``POST /api/v1/jira/ticket/transition`` for each one.

The drain is intentionally separated from
``_persist_phase_gate_resolution`` so the HITL POST returns within
its existing latency SLA (slice-2 task-2-7 acceptance criterion).
Per-Task ``jira_action_status`` flips to ``'applied'`` on success or
``'failed'`` on each transition; the failure reason lands in
``Task.notes``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

logger = logging.getLogger(__name__)


_DRAIN_TIMEOUT_SECONDS = 30


@dataclass
class WontDoEntry:
    """A single Won't-Do transition the orchestrator should drain.

    Fields are intentionally permissive — the applier emits whatever
    structure helps the operator audit the batch, but the only fields
    the drain itself reads are ``jira_key`` and ``comment``.
    """

    jira_key: str
    comment: str = ""
    task_id: str | None = None
    survivor_key: str | None = None  # for consolidate-into pointers


@dataclass
class DrainResult:
    """Per-entry outcome of one ``run_wontdo_drain`` invocation."""

    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (key, reason)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def _resolve_launcher_secret() -> str:
    """Mirror of :func:`orchestrator.jira_epic._resolve_launcher_secret`."""
    mount_path = "/secrets/launcher-secret"
    try:
        with open(mount_path, encoding="utf-8") as fh:
            secret = fh.read().strip()
            if secret:
                return secret
    except OSError:
        pass
    return os.environ.get("EGG_LAUNCHER_SECRET", "")


def _gateway_base_url() -> str:
    explicit = os.environ.get("EGG_GATEWAY_URL", "").rstrip("/")
    if explicit:
        return explicit
    host = os.environ.get("GATEWAY_HOST", "gateway.egg-system.svc.cluster.local")
    port = os.environ.get("GATEWAY_PORT", "9848")  # noqa: EGG002
    return f"http://{host}:{port}"


def _post_transition(
    *,
    jira_key: str,
    comment: str,
    transition_name: str = "Won't Do",
) -> tuple[bool, str]:
    """POST ``/api/v1/jira/ticket/transition`` for one ticket.

    Returns ``(ok, reason)``. Failures fail closed — the caller flips
    the per-Task lifecycle to ``'failed'`` and records the reason in
    ``Task.notes`` so the operator can retry.
    """
    url = f"{_gateway_base_url()}/api/v1/jira/ticket/transition"
    body = {
        "ticket": jira_key,
        "transition_name": transition_name,
    }
    if comment:
        body["comment"] = comment
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    launcher = _resolve_launcher_secret()
    if launcher:
        headers["Authorization"] = f"Bearer {launcher}"
    opener = build_opener()
    req = Request(url, data=payload, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=_DRAIN_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
            if response.status < 200 or response.status >= 300:
                return False, f"upstream_status={response.status}; body={raw[:200]}"
            return True, ""
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            raw = ""
        return False, f"http_error_{exc.code}; body={raw[:200]}"
    except (URLError, OSError) as exc:
        return False, f"transport_error={exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"unexpected_error={exc}"


def load_wontdo_handoff(path: Path) -> list[WontDoEntry]:
    """Parse a Won't-Do handoff JSON file produced by the APPLIER.

    The applier writes a list of entries each carrying at minimum a
    ``jira_key`` field. Missing files / malformed JSON / unexpected
    shapes return an empty list — the drain treats absence as
    "nothing to do" rather than failing the pipeline.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Won't-Do drain: cannot read %s — %s", path, exc)
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Won't-Do drain: invalid JSON in %s — %s",
            path,
            exc,
        )
        return []

    # Accept either a bare list ``[{...}, {...}]`` or a wrapped
    # ``{"entries": [...], "epic_key": "..."}``.
    if isinstance(data, dict):
        entries_raw = data.get("entries")
    elif isinstance(data, list):
        entries_raw = data
    else:
        entries_raw = []

    if not isinstance(entries_raw, list):
        return []

    entries: list[WontDoEntry] = []
    for entry in entries_raw:
        if not isinstance(entry, dict):
            continue
        jira_key = entry.get("jira_key") or entry.get("key") or ""
        if not isinstance(jira_key, str) or not jira_key.strip():
            continue
        entries.append(
            WontDoEntry(
                jira_key=jira_key.strip(),
                comment=str(entry.get("comment") or "").strip(),
                task_id=str(entry.get("task_id")) if entry.get("task_id") else None,
                survivor_key=(
                    str(entry.get("survivor_key")) if entry.get("survivor_key") else None
                ),
            )
        )
    return entries


def run_wontdo_drain(
    *,
    handoff_path: Path,
    on_entry_result: Any = None,
) -> DrainResult:
    """Drain a Won't-Do handoff file via the gateway ``/transition`` route.

    Parameters
    ----------
    handoff_path:
        Filesystem path to the JSON file the APPLIER wrote
        (``.egg-state/agent-outputs/<pipeline>-wontdo.json``).
    on_entry_result:
        Optional callback invoked as
        ``on_entry_result(entry: WontDoEntry, ok: bool, reason: str)``
        after each transition attempt. Used by the orchestrator to
        flip per-Task ``jira_action_status`` and record failure
        reasons in ``Task.notes``. When ``None``, results are only
        accumulated into the returned ``DrainResult``.

    Returns
    -------
    :class:`DrainResult`
        Aggregated outcome. Idempotent on re-run — succeeding
        transitions don't double-fire because the gateway's
        idempotency cache rejects repeats within
        ``IDEMPOTENCY_TTL_SECONDS``; failing ones can be retried by
        the operator after addressing the underlying error.
    """
    result = DrainResult()
    entries = load_wontdo_handoff(handoff_path)
    if not entries:
        return result

    for entry in entries:
        ok, reason = _post_transition(
            jira_key=entry.jira_key,
            comment=entry.comment,
        )
        if ok:
            result.succeeded.append(entry.jira_key)
        else:
            result.failed.append((entry.jira_key, reason))
            logger.warning(
                "Won't-Do drain: transition failed for %s — %s",
                entry.jira_key,
                reason,
            )
        if on_entry_result is not None:
            try:
                on_entry_result(entry, ok, reason)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(
                    "Won't-Do drain: callback raised for %s — %s",
                    entry.jira_key,
                    exc,
                )
    return result


__all__ = [
    "DrainResult",
    "WontDoEntry",
    "load_wontdo_handoff",
    "run_wontdo_drain",
]
