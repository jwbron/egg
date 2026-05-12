"""
Orchestrator-direct Jira transitions client (issue #1557 TASK-1-5).

The agent-facing gateway permanently blocks the ``transitions`` API path
(see ``gateway/jira_client.py`` — ``ALLOWED_METHODS = frozenset({"GET"})``
and the path-segment denylist).  Won't-Do transitions therefore run from
the orchestrator process directly against Atlassian Cloud, using the same
``~/.config/egg/secrets.env`` credentials the gateway loads.

Trust boundary:

The orchestrator container has access to Atlassian credentials by design
(it runs the sandbox-spawning loop and never exposes its cred surface to
agents).  The credential file is shared with the gateway via volume
mount so there is no second copy of secrets to manage.  This module is
the **only** legitimate caller of denylisted Jira write paths from
outside ``gateway/``; the regression test
``orchestrator/tests/test_no_outbound_jira_writes.py`` (TASK-1-18 / R7
mitigation) enforces the invariant.

Feature flag:

Per risk_analyst R1, orchestrator-direct transitions are opt-in.  The
client raises :class:`OrchJiraTransitionsDisabled` unless
``EGG_ENABLE_ORCH_JIRA_TRANSITIONS=true`` is set in the environment.
Won't-Do batches (TASK-1-14) translate that exception into a HITL gate
asking the operator to enable the flag.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

# Add shared directory to path for egg_jira_credentials / egg_logging.
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


from egg_jira_credentials import (  # noqa: E402
    JiraCredentials,
    JiraCredentialsUnavailable,
    get_jira_credentials,
)

logger = get_logger("orchestrator.jira_transitions")


WONT_DO_NAMES: frozenset[str] = frozenset({"won't do", "wont do", "won't fix"})
"""Lower-cased transition / status names that satisfy 'Won't Do' for our purposes.

Different Jira projects spell it differently — the Atlassian default is
"Won't Do", but legacy Jira Server projects often have "Won't Fix". The
match is case-insensitive and matches either the transition name (used
for the API call) or the post-transition status name (used for the
already-in-state short-circuit).
"""


class OrchJiraTransitionsDisabled(RuntimeError):
    """Raised when the orchestrator-direct transitions feature flag is off.

    Callers should surface this as an operator HITL gate asking whether
    to enable ``EGG_ENABLE_ORCH_JIRA_TRANSITIONS`` and retry, or skip the
    Won't-Do step entirely.
    """


class JiraTransitionFailed(RuntimeError):
    """Raised when an Atlassian transitions call fails (4xx/5xx).

    The caller (TASK-1-14 Won't-Do batch) records the per-entry error on
    :class:`EpicApplyWontDoEntry.error` so the operator can see partial
    state after a network blip.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a single :meth:`JiraTransitionsClient.transition_to_wont_do` call."""

    status: Literal["applied", "already_in_state", "transition_not_found"]
    child_key: str
    from_status: str
    to_status: str | None
    transition_id: str | None


def _feature_flag_enabled() -> bool:
    raw = os.environ.get("EGG_ENABLE_ORCH_JIRA_TRANSITIONS", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _wrap_text_as_adf(text: str) -> dict[str, Any]:
    """Wrap a plain-text string in Atlassian Document Format (ADF).

    Atlassian REST API v3 rejects raw strings for issue-comment bodies
    (#1557 reviewer_code v3 #8); every comment must be an ADF document.
    This is a minimal wrapper — paragraph node with one text leaf — that
    matches the gateway-side ``gateway/jira_adf.py::wrap_text_as_adf``
    contract.  We re-implement here rather than importing because the
    orchestrator-direct transitions client lives outside the gateway
    package boundary by design (decision-11).
    """
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": text},
                ],
            }
        ],
    }


class JiraTransitionsClient:
    """Thin client that issues Atlassian ``/transitions`` calls.

    Uses Basic auth from :func:`egg_jira_credentials.get_jira_credentials`
    so the gateway and orchestrator share a single secrets surface.

    The client deliberately avoids any HTTP retry logic — transitions are
    a write verb with at-most-once semantics; on 4xx/5xx we surface the
    failure to the caller and let the operator decide.
    """

    def __init__(
        self,
        creds_provider: Any = get_jira_credentials,
        *,
        http_client: Any | None = None,
        timeout_seconds: float = 30.0,
    ):
        self._creds_provider = creds_provider
        self._http_client = http_client  # optional pre-built httpx.Client for tests
        self._timeout = timeout_seconds
        self._transition_cache: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    def transition_to_wont_do(
        self,
        child_key: str,
        comment: str,
        *,
        epic_key: str | None = None,
    ) -> TransitionResult:
        """Transition ``child_key`` to a Won't-Do-ish status.

        Idempotent re-runs (reviewer_code v3 #7): checks BOTH
        ``status.statusCategory.key == "done"`` AND
        ``resolution.name`` against the Won't-Do set so the most common
        Atlassian workflow shape ("Done" with ``resolution="Won't Do"``)
        short-circuits cleanly.

        Audit logging (reviewer_security v3 #14): emits one structured
        ``orch_jira_transition_attempt`` log line at the start of every
        attempt AND on every exit path (success / already-in-state /
        transition-not-found / credentials-unavailable / 4xx / 5xx),
        so operators have a paper trail regardless of outcome.
        """
        # Always emit a pre-flight audit line so failure paths are
        # captured. Subsequent log lines carry the outcome status.
        principal = "<unknown>"
        try:
            creds = self._creds_provider()
            principal = creds.username
        except JiraCredentialsUnavailable as exc:
            logger.warning(
                "orch_jira_transition_attempt",
                epic_key=epic_key,
                child_key=child_key,
                from_status=None,
                to_status=None,
                transition_id=None,
                principal=principal,
                outcome="credentials_unavailable",
                error=str(exc),
            )
            raise

        if not _feature_flag_enabled():
            logger.warning(
                "orch_jira_transition_attempt",
                epic_key=epic_key,
                child_key=child_key,
                from_status=None,
                to_status=None,
                transition_id=None,
                principal=principal,
                outcome="feature_flag_disabled",
            )
            raise OrchJiraTransitionsDisabled(
                "EGG_ENABLE_ORCH_JIRA_TRANSITIONS is not set; orchestrator-"
                "direct Jira transitions are opt-in (risk_analyst R1)."
            )

        # 1. Inspect current status AND resolution (idempotency short-circuit).
        try:
            current, current_category, current_resolution = self._get_current_state(
                creds, child_key
            )
        except JiraTransitionFailed as exc:
            logger.warning(
                "orch_jira_transition_attempt",
                epic_key=epic_key,
                child_key=child_key,
                from_status=None,
                to_status=None,
                transition_id=None,
                principal=principal,
                outcome="status_fetch_failed",
                error=str(exc),
            )
            raise

        # Already-in-state per reviewer_code v3 #7: the common Atlassian
        # workflow has ``status="Done"`` + ``resolution="Won't Do"``.
        current_lower = (current or "").lower()
        resolution_lower = (current_resolution or "").lower()
        is_wont_do = current_lower in WONT_DO_NAMES or (
            current_category == "done" and resolution_lower in WONT_DO_NAMES
        )
        if is_wont_do:
            logger.info(
                "orch_jira_transition_attempt",
                epic_key=epic_key,
                child_key=child_key,
                from_status=current,
                to_status=current,
                transition_id=None,
                principal=principal,
                outcome="already_in_state",
                resolution=current_resolution,
            )
            return TransitionResult(
                status="already_in_state",
                child_key=child_key,
                from_status=current,
                to_status=current,
                transition_id=None,
            )

        # 2. Look up the Won't-Do transition id (project-cached).
        project_key = child_key.split("-", 1)[0]
        transition_id = self._resolve_wont_do_transition_id(creds, child_key, project_key)
        if transition_id is None:
            logger.warning(
                "orch_jira_transition_attempt",
                epic_key=epic_key,
                child_key=child_key,
                from_status=current,
                to_status=None,
                transition_id=None,
                principal=principal,
                outcome="transition_not_found",
                error="wont_do_transition_not_available",
            )
            return TransitionResult(
                status="transition_not_found",
                child_key=child_key,
                from_status=current,
                to_status=None,
                transition_id=None,
            )

        # 3. POST the transition. Comment body is wrapped in ADF per
        #    reviewer_code v3 #8 — Atlassian REST API v3 rejects raw
        #    strings for issue-comment bodies.
        body: dict[str, Any] = {"transition": {"id": transition_id}}
        stripped_comment = comment.strip()
        if stripped_comment:
            body["update"] = {
                "comment": [{"add": {"body": _wrap_text_as_adf(stripped_comment)}}],
            }

        try:
            self._post_transition(creds, child_key, body)
        except JiraTransitionFailed as exc:
            logger.warning(
                "orch_jira_transition_attempt",
                epic_key=epic_key,
                child_key=child_key,
                from_status=current,
                to_status=None,
                transition_id=transition_id,
                principal=principal,
                outcome="post_failed",
                error=str(exc),
            )
            raise

        # 4. Re-fetch to confirm the new status (defence in depth — partial
        #    workflow definitions sometimes accept a transition but leave
        #    the ticket in the prior state).
        try:
            new_status, _new_category, _new_resolution = self._get_current_state(creds, child_key)
        except JiraTransitionFailed:
            new_status = None
        logger.info(
            "orch_jira_transition_attempt",
            epic_key=epic_key,
            child_key=child_key,
            from_status=current,
            to_status=new_status,
            transition_id=transition_id,
            principal=principal,
            outcome="applied",
        )

        return TransitionResult(
            status="applied",
            child_key=child_key,
            from_status=current,
            to_status=new_status or "",
            transition_id=transition_id,
        )

    def close(self) -> None:
        """Close the underlying httpx.Client (#1557 reviewer_security #11).

        Long-running orchestrator processes accumulate connection pools
        if the client is never closed; expose an explicit close hook
        for orchestrator shutdown.  Safe to call multiple times.
        """
        with self._lock:
            client = self._http_client
            self._http_client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — defensive: don't raise on shutdown
                pass

    def __enter__(self) -> JiraTransitionsClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------
    def _client(self):  # type: ignore[no-untyped-def]
        """Return the HTTP client used for Atlassian requests.

        We resolve the httpx import lazily so this module can be imported
        in environments where httpx isn't installed (the gateway already
        depends on httpx in production).

        Concurrency (reviewer_concurrency v3 non-blocking finding):
            Double-checked locking under ``self._lock`` so two threads
            calling :meth:`transition_to_wont_do` concurrently can't
            each instantiate their own ``httpx.Client`` and orphan one
            (the orphaned client's connection pool would persist until
            GC reclaims it).
        """
        client = self._http_client
        if client is not None:
            return client
        import httpx  # noqa: PLC0415

        with self._lock:
            if self._http_client is None:
                self._http_client = httpx.Client(timeout=self._timeout)
            return self._http_client

    @staticmethod
    def _headers(creds: JiraCredentials) -> dict[str, str]:
        return {
            "Authorization": creds.basic_auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get_current_status(self, creds: JiraCredentials, child_key: str) -> str:
        """Backward-compat shim for callers that only need the status name."""
        status, _category, _resolution = self._get_current_state(creds, child_key)
        return status

    def _get_current_state(
        self, creds: JiraCredentials, child_key: str
    ) -> tuple[str, str | None, str | None]:
        """Fetch ``(status_name, status_category_key, resolution_name)``.

        Per #1557 reviewer_code v3 #7: the idempotency short-circuit
        must check the ``resolution`` field too because the most common
        Atlassian "Won't Do" workflow shape leaves ``status="Done"``
        with ``resolution="Won't Do"``.  Returning the status_category
        in addition lets the caller distinguish "Done category +
        Won't-Do resolution" from "Done category + Fixed resolution".
        """
        url = (
            f"{creds.base_url}/rest/api/3/issue/{quote(child_key, safe='')}"
            "?fields=status,resolution"
        )
        client = self._client()
        # Brief retry on 429 to honour Atlassian rate-limits politely.
        for attempt in (0, 1):
            response = client.get(url, headers=self._headers(creds))
            if response.status_code == 429 and attempt == 0:
                retry_after = float(response.headers.get("Retry-After", "1") or 1)
                time.sleep(min(retry_after, 30.0))
                continue
            break
        if response.status_code != 200:
            raise JiraTransitionFailed(
                f"Failed to fetch status for {child_key}: HTTP "
                f"{response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        body = response.json()
        fields = body.get("fields") or {}
        status_block = fields.get("status") or {}
        status_name = status_block.get("name") or "Unknown"
        category_block = status_block.get("statusCategory") or {}
        category_key = category_block.get("key")
        resolution_block = fields.get("resolution") or {}
        resolution_name = (
            resolution_block.get("name") if isinstance(resolution_block, dict) else None
        )
        return status_name, category_key, resolution_name

    def _resolve_wont_do_transition_id(
        self, creds: JiraCredentials, child_key: str, project_key: str
    ) -> str | None:
        """Return the Atlassian transition id matching a Won't-Do-ish name.

        Caches per-project so a batch of N children only issues one
        ``GET /transitions`` per project.  Falls back to a per-child fetch
        when a child's workflow doesn't match the cached set (rare —
        operators occasionally customise child workflows).
        """
        with self._lock:
            cached = self._transition_cache.get(project_key)

        if cached is None:
            transitions = self._fetch_transitions(creds, child_key)
            with self._lock:
                # Only cache when we got at least one transition — an empty
                # list often means the ticket is in a state with no outgoing
                # transitions, which is per-ticket, not per-project.
                if transitions:
                    self._transition_cache[project_key] = transitions
            cached = transitions

        for name, transition_id in cached.items():
            if name.lower() in WONT_DO_NAMES:
                return transition_id
        return None

    def _fetch_transitions(self, creds: JiraCredentials, child_key: str) -> dict[str, str]:
        url = f"{creds.base_url}/rest/api/3/issue/{quote(child_key, safe='')}/transitions"
        client = self._client()
        for attempt in (0, 1):
            response = client.get(url, headers=self._headers(creds))
            if response.status_code == 429 and attempt == 0:
                retry_after = float(response.headers.get("Retry-After", "1") or 1)
                time.sleep(min(retry_after, 30.0))
                continue
            break
        if response.status_code != 200:
            raise JiraTransitionFailed(
                f"Failed to list transitions for {child_key}: HTTP "
                f"{response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        body = response.json()
        result: dict[str, str] = {}
        for entry in body.get("transitions", []):
            name = entry.get("name")
            tid = entry.get("id")
            if isinstance(name, str) and isinstance(tid, str):
                result[name] = tid
        return result

    def _post_transition(
        self, creds: JiraCredentials, child_key: str, body: dict[str, Any]
    ) -> None:
        # Defence in depth (#1557 reviewer_security v3 #15): the
        # feature flag is also enforced here so future callers that
        # invoke ``_post_transition`` directly without going through
        # ``transition_to_wont_do`` can't bypass the opt-in posture.
        if not _feature_flag_enabled():
            raise OrchJiraTransitionsDisabled(
                "EGG_ENABLE_ORCH_JIRA_TRANSITIONS is not set; "
                "_post_transition refusing to dispatch the write."
            )
        url = f"{creds.base_url}/rest/api/3/issue/{quote(child_key, safe='')}/transitions"
        client = self._client()
        response = client.post(url, headers=self._headers(creds), json=body)
        # 204 = success (per Atlassian docs).  200 sometimes returned on
        # very old projects, so accept the 2xx range.
        if not (200 <= response.status_code < 300):
            raise JiraTransitionFailed(
                f"Transition POST for {child_key} failed: HTTP "
                f"{response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )

    def invalidate_transition_cache(self, project_key: str | None = None) -> None:
        """Drop cached transition ids for a project (or all)."""
        with self._lock:
            if project_key is None:
                self._transition_cache.clear()
            else:
                self._transition_cache.pop(project_key, None)


__all__ = [
    "JiraTransitionFailed",
    "JiraTransitionsClient",
    "OrchJiraTransitionsDisabled",
    "TransitionResult",
    "WONT_DO_NAMES",
]
