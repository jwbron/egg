"""HTTP client for the orchestrator's commit-authorship registry.

The gateway makes two kinds of calls against the registry:

- ``register(...)`` is fired by the commit observer every time
  ``/api/v1/git/execute`` creates one or more new commits.  Best-effort
  — on failure, we log at WARNING and return; the push-time lookup will
  subsequently fail-closed for the missing SHAs (safe by construction).
- ``lookup_bulk(...)`` is called by the push handler before a push to
  partition the diff by author role.  Failure here is also fail-closed
  — the caller treats every sha as unregistered, which the push
  handler's rewrite logic then treats as own-authored (the conservative
  direction).

The gateway pod carries ``EGG_LIFECYCLE_SECRET`` alongside the
orchestrator pod so the registry endpoints can run under the same
``require_lifecycle_secret`` decorator used by other
authorization-affecting routes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
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


logger = get_logger("gateway.commit_registry_client")

_DEFAULT_ORCHESTRATOR_URL = "http://egg-orchestrator:9849"
_DEFAULT_TIMEOUT_SECONDS = 5  # observer calls are non-blocking; keep tight
_DEFAULT_LOOKUP_TIMEOUT_SECONDS = 10  # lookups gate pushes; slightly longer


def _orchestrator_url() -> str:
    return os.environ.get("EGG_ORCHESTRATOR_URL", _DEFAULT_ORCHESTRATOR_URL).rstrip("/")


def _auth_header() -> dict[str, str]:
    secret = os.environ.get("EGG_LIFECYCLE_SECRET", "")
    if not secret:
        return {}
    return {"Authorization": f"Bearer {secret}"}


def _post(
    path: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """POST JSON to the orchestrator; return (status, json_body, error)."""
    url = f"{_orchestrator_url()}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(_auth_header())
    headers.setdefault("X-Egg-Source", "gateway")

    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError:
                parsed = None
            return resp.status, parsed, None
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError, Exception:
            parsed = None
        return exc.code, parsed, f"HTTPError {exc.code}"
    except (URLError, TimeoutError) as exc:
        return 0, None, f"Network error: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return 0, None, f"Unexpected error: {exc}"


class CommitRegistryClient:
    """Thin wrapper around the orchestrator's authorship routes."""

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        lookup_timeout: float = _DEFAULT_LOOKUP_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout = timeout
        self._lookup_timeout = lookup_timeout

    def register(
        self,
        sha: str,
        role: str,
        pipeline_id: str | None,
        *,
        repo: str | None = None,
        branch: str | None = None,
    ) -> bool:
        """Register one commit.  Returns True on 200/409 (both benign).

        A 409 means the SHA is already bound to a different role — the
        observer doesn't get to override that (first-wins at the store
        level).  Either way, the caller doesn't care: the registration
        is "durably decided" from the gateway's perspective.
        """
        payload = {
            "sha": sha,
            "role": role,
            "pipeline_id": pipeline_id,
            "repo": repo,
            "branch": branch,
        }
        status, _body, err = _post(
            "/api/v1/commit-authorship/register",
            payload,
            timeout=self._timeout,
        )
        if status == 200:
            return True
        if status == 409:
            logger.warning(
                "commit_authorship_register_collision",
                sha=sha,
                attempted_role=role,
            )
            return True
        logger.warning(
            "commit_authorship_register_failed",
            sha=sha,
            role=role,
            status=status,
            error=err,
        )
        return False

    def register_bulk(
        self,
        items: list[dict[str, Any]],
    ) -> bool:
        """Register many commits in one HTTP round-trip. Best-effort."""
        if not items:
            return True
        status, _body, err = _post(
            "/api/v1/commit-authorship/register-bulk",
            {"items": items},
            timeout=self._timeout,
        )
        if status == 200:
            return True
        logger.warning(
            "commit_authorship_register_bulk_failed",
            count=len(items),
            status=status,
            error=err,
        )
        return False

    def lookup_bulk(self, shas: list[str]) -> dict[str, str | None]:
        """Look up attribution for a batch of SHAs.

        Returns an empty dict on network/server failures; the caller
        interprets missing entries as "unregistered" (fail-closed).
        """
        if not shas:
            return {}
        status, body, err = _post(
            "/api/v1/commit-authorship/lookup",
            {"shas": list(shas)},
            timeout=self._lookup_timeout,
        )
        if status != 200 or not isinstance(body, dict):
            logger.warning(
                "commit_authorship_lookup_failed",
                count=len(shas),
                status=status,
                error=err,
            )
            return {}
        attribution = body.get("attribution", {})
        if not isinstance(attribution, dict):
            return {}
        # Normalize: ensure every requested sha is present (None means
        # unregistered), and drop any alien keys the server may emit.
        out: dict[str, str | None] = {}
        for sha in shas:
            value = attribution.get(sha)
            if value is None or isinstance(value, str):
                out[sha] = value
        return out


_client: CommitRegistryClient | None = None


def get_client() -> CommitRegistryClient:
    """Return a module-level singleton.  Tests can ``reset_client()``."""
    global _client
    if _client is None:
        _client = CommitRegistryClient()
    return _client


def reset_client() -> None:
    """Drop the module singleton.  Intended for tests only."""
    global _client
    _client = None
