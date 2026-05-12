"""
Jira-epic detection helper (issue #1557 task-1-1).

The orchestrator's ``POST /api/v1/pipelines`` route consults this
module to decide whether a freshly submitted Jira ticket should run
the **epic-mode** SDLC pipeline (refine → plan → apply → implement)
rather than the default ticket pipeline (refine → plan → implement).
It is intentionally tiny and dependency-light so the create-pipeline
hot path takes a small, predictable hit on the rare epic-mode call.

How detection works
-------------------
``is_epic_for_ticket(ticket)`` calls the gateway's existing
``POST /api/v1/jira/ticket/get`` route with
``fields=['issuetype', 'status', 'description', 'summary', 'parent']``
and inspects ``issuetype.name`` for the literal string ``"Epic"``
(case-insensitive). It returns a tuple ``(is_epic, payload)`` so the
caller can re-use the fetched payload for downstream work (e.g.
seeding the refiner's analysis with the epic's current Description).

``probe_epic_children(ticket, project)`` calls the gateway's
``POST /api/v1/jira/search`` with the JQL
``project = <P> AND parent = <K>`` and ``maxResults=1`` to cheaply
test whether the epic already has at least one child. The orchestrator
uses this to resolve ``mode='auto'`` to ``'reassess'`` (children
present) or ``'fresh'`` (none).

Both helpers fail open: any non-2xx response or transport error
returns ``(False, {})`` / ``False`` so a Jira outage does not block
non-epic pipelines.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

logger = logging.getLogger(__name__)


_EPIC_DETECTION_TIMEOUT_SECONDS = 10
_EPIC_DETECTION_FIELDS = (
    "issuetype",
    "status",
    "description",
    "summary",
    "parent",
)


def _resolve_launcher_secret() -> str:
    """Read the orchestrator's launcher secret.

    Mirrors the gateway-side resolution: tries
    ``/secrets/launcher-secret`` first (the in-cluster mount) before
    falling back to the ``EGG_LAUNCHER_SECRET`` env var that local
    dev setups use.
    """
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
    """Resolve the gateway base URL the orchestrator should talk to.

    Tries ``EGG_GATEWAY_URL`` first; falls back to the
    ``GATEWAY_HOST``/``GATEWAY_PORT`` env pair that ``GatewayClient``
    uses, then the canonical in-cluster service name.
    """
    explicit = os.environ.get("EGG_GATEWAY_URL", "").rstrip("/")
    if explicit:
        return explicit
    host = os.environ.get("GATEWAY_HOST", "gateway.egg-system.svc.cluster.local")
    port = os.environ.get("GATEWAY_PORT", "9848")  # noqa: EGG002
    return f"http://{host}:{port}"


def _gateway_post(path: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Issue a JSON POST to the gateway and return the decoded body.

    Raises on transport error. Treats non-2xx as JSON-decoded errors —
    callers should catch broadly.
    """
    url = f"{_gateway_base_url()}{path}"
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    # Forward the orchestrator's launcher secret as a bearer token so
    # the gateway's session-or-launcher auth path treats the call as
    # orchestrator-internal rather than agent-facing. The gateway's
    # private-mode check + project allowlist remain the hard boundary.
    launcher = _resolve_launcher_secret()
    if launcher:
        headers["Authorization"] = f"Bearer {launcher}"
    opener = build_opener()
    req = Request(url, data=payload, headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        if not raw:
            return {}
        return json.loads(raw)


def is_epic_for_ticket(ticket: str) -> tuple[bool, dict[str, Any]]:
    """Return ``(is_epic, payload)`` for the named Jira ticket.

    Fails open: on any error the result is ``(False, {})`` so the
    pipeline falls back to the default ticket flow. The payload is
    the raw gateway response (whatever ``ticket/get`` returned for
    the requested fields).

    Parameters
    ----------
    ticket:
        Atlassian Jira ticket key (e.g. ``"ENG-1234"``). Must already
        be normalised to upper-case; the function does NOT re-validate.
    """
    if not ticket:
        return False, {}
    try:
        response = _gateway_post(
            "/api/v1/jira/ticket/get",
            {"ticket": ticket, "fields": list(_EPIC_DETECTION_FIELDS)},
            timeout=_EPIC_DETECTION_TIMEOUT_SECONDS,
        )
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Epic detection: failed to fetch Jira ticket %s — %s; treating as non-epic",
            ticket,
            exc,
        )
        return False, {}
    # Gateway responses look like ``{"success": true, "data": {...}}``
    # with the issue payload under ``data``. Be defensive — accept both
    # the wrapped and unwrapped shapes.
    payload: dict[str, Any] = response.get("data") or response
    fields = payload.get("fields") or {}
    issuetype = fields.get("issuetype") or {}
    name = issuetype.get("name", "")
    if isinstance(name, str) and name.strip().lower() == "epic":
        return True, payload
    return False, payload


def probe_epic_children(ticket: str, project: str) -> bool:
    """Return True if the named epic already has at least one child.

    Implementation: ``project = <P> AND parent = <K>`` JQL with
    ``maxResults=1``. Fails open: on any error returns False, which
    pushes ``mode='auto'`` to ``'fresh'``.
    """
    if not ticket or not project:
        return False
    jql = f"project = {project} AND parent = {ticket}"
    try:
        response = _gateway_post(
            "/api/v1/jira/search",
            {"jql": jql, "maxResults": 1, "fields": ["summary"]},
            timeout=_EPIC_DETECTION_TIMEOUT_SECONDS,
        )
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Epic children probe: failed for %s — %s; assuming no children",
            ticket,
            exc,
        )
        return False
    data: dict[str, Any] = response.get("data") or response
    issues = data.get("issues")
    if not issues:
        return False
    return isinstance(issues, list) and len(issues) > 0


def resolve_epic_mode(
    *,
    ticket: str | None,
    epic_mode_arg: str | None,
) -> tuple[bool, str | None, list[str]]:
    """Resolve ``(is_epic, pipeline_mode, warnings)`` for a submit call.

    Implements the canonical decision tree from issue #1557 task-1-1:

    - ``ticket is None`` → ``(False, None, [])`` (no Jira footprint).
    - ``epic_mode_arg == 'fresh'`` → forces ``is_epic=True,
      pipeline_mode='fresh'`` after verifying issuetype is Epic; a
      ``'fresh'`` against an epic that already has children emits a
      warning but proceeds.
    - ``epic_mode_arg == 'reassess'`` → forces
      ``is_epic=True, pipeline_mode='reassess'`` — caller must reject
      with HTTP 400 when ``is_epic_for_ticket`` returned False.
    - ``epic_mode_arg in (None, 'auto')`` → autodetect via the helpers
      above.

    Returns
    -------
    (is_epic, pipeline_mode, warnings)
        ``warnings`` is a list of human-readable strings the caller
        should surface in the API response (e.g. via a ``warnings``
        field on the 201 payload). Non-empty even when the call
        succeeds — these are advisory, not errors.
    """
    if not ticket:
        return False, None, []

    arg = (epic_mode_arg or "auto").lower()
    is_epic, _ = is_epic_for_ticket(ticket)
    project = ticket.split("-", 1)[0] if "-" in ticket else ""

    warnings: list[str] = []

    if arg == "reassess":
        if not is_epic:
            warnings.append(
                f"epic_mode='reassess' but ticket {ticket!r} is not an "
                "Epic; refusing to force reassess mode"
            )
            return False, None, warnings
        return True, "reassess", warnings

    if arg == "fresh":
        if not is_epic:
            warnings.append(
                f"epic_mode='fresh' but ticket {ticket!r} is not an "
                "Epic; falling back to standard ticket mode"
            )
            return False, None, warnings
        if project and probe_epic_children(ticket, project):
            warnings.append(
                f"epic_mode='fresh' but epic {ticket!r} already has "
                "children; proceeding anyway (operator override)"
            )
        return True, "fresh", warnings

    # auto
    if not is_epic:
        return False, None, warnings
    has_children = bool(project and probe_epic_children(ticket, project))
    return True, ("reassess" if has_children else "fresh"), warnings


__all__ = [
    "is_epic_for_ticket",
    "probe_epic_children",
    "resolve_epic_mode",
]
