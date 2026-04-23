"""Gateway-inline commit observer.

The gateway is the only code path that creates commits for agent
sessions (sandbox containers cannot touch their own ``.git``; the
gateway's ``/api/v1/git/execute`` is the sole route).  So we observe
commits there: snapshot HEAD before a git subcommand, snapshot HEAD
after, and register each new SHA with the orchestrator's authorship
registry.

See `.egg-state/agent-outputs/1882-architect-output.json` for why this
replaces the sandbox-side ``post-commit`` hook described in
decision-1(d): agents can suppress hooks with ``--no-verify``, and the
sandbox has no real ``.git`` to hook against (it's a tmpfs shadow).
The gateway-inline observer is bypass-proof and covers *every*
commit-creating subcommand in one place.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterable
from typing import Any

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover

    def get_logger(  # type: ignore[misc]
        name: str,
        level: int | str = logging.INFO,
        component: str | None = None,
    ) -> logging.Logger:
        return logging.getLogger(name)


logger = get_logger("gateway.commit_observer")


def _rev_list_between(exec_path: str, before: str | None, after: str) -> list[str]:
    """Return the set of new commits between ``before`` and ``after``.

    When ``before`` is empty/None (e.g., the pre-commit HEAD was on an
    unborn branch), we fall back to enumerating reachable commits
    bounded by sensible remote refs so we don't re-register the whole
    history.  The fallback conservatively returns ``[after]`` when no
    other anchor is known.
    """
    after_s = (after or "").strip()
    if not after_s:
        return []

    if before and before.strip() and before.strip() != after_s:
        cmd = ["git", "rev-list", f"{before.strip()}..{after_s}"]
    else:
        # Unborn-branch case: register just the new HEAD commit.
        # This is conservative and avoids walking an unbounded history
        # on the first commit of a branch.  Callers (e.g. merge/rebase)
        # that care about deeper ranges supply a non-empty ``before``.
        return [after_s]

    try:
        result = subprocess.run(
            cmd,
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "commit_observer_rev_list_timeout",
            before=before,
            after=after_s,
        )
        return []
    except Exception as exc:
        logger.warning(
            "commit_observer_rev_list_failed",
            before=before,
            after=after_s,
            error=str(exc),
        )
        return []

    if result.returncode != 0:
        # When ``before`` has been garbage-collected or refers to
        # something git no longer recognises (e.g. after ``rebase``
        # rewriting), rev-list exits non-zero.  Register only the tip
        # so the agent's own commit is still attributed.
        logger.debug(
            "commit_observer_rev_list_nonzero",
            before=before,
            after=after_s,
            stderr=(result.stderr or "").strip(),
        )
        return [after_s]

    shas: list[str] = []
    for line in (result.stdout or "").splitlines():
        s = line.strip()
        if s:
            shas.append(s)
    return shas


def capture_head(exec_path: str) -> str | None:
    """Return ``git rev-parse HEAD`` in ``exec_path`` or ``None``.

    Used to snapshot HEAD before/after a ``/api/v1/git/execute`` call.
    Returns ``None`` on any failure — the unborn-branch case, a missing
    worktree, etc.  Callers interpret ``None`` as "no prior HEAD", which
    disables the diff-only walk in ``observe``.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    sha = (result.stdout or "").strip()
    return sha or None


def observe(
    exec_path: str,
    *,
    before_head: str | None,
    after_head: str | None,
    branch: str | None,
    session_role: str | None,
    pipeline_id: str | None,
    repo: str | None,
    registry_client: Any | None = None,
) -> list[str]:
    """Register any commits created between ``before_head`` and ``after_head``.

    Returns the list of SHAs we attempted to register (may be empty).

    Failures are logged at WARNING and swallowed; the caller must not
    let them affect the response the agent sees.
    """
    if not session_role:
        # Non-agent session (e.g. internal gateway git op) — observer does nothing.
        return []
    if not after_head:
        return []
    if before_head == after_head:
        # Operation did not advance HEAD (e.g. ``git status``).
        return []

    shas = _rev_list_between(exec_path, before_head, after_head)
    if not shas:
        return []

    # Lazily resolve the client so callers (gateway.py) and tests can
    # override via dependency injection without the observer having to
    # know about the singleton.
    client = registry_client
    if client is None:
        import sys as _sys

        _crc_mod = _sys.modules.get("commit_registry_client") or _sys.modules.get(
            "gateway.commit_registry_client"
        )
        get_client = getattr(_crc_mod, "get_client", None) if _crc_mod else None
        if get_client is None:
            try:
                # mypy: the dynamic loader below is a fallback path; the
                # primary path finds the module already imported.
                import commit_registry_client as _crc  # type: ignore[import-untyped]

                get_client = _crc.get_client
            except ImportError:  # pragma: no cover
                try:
                    import importlib.util as _util
                    from pathlib import Path as _Path

                    _p = _Path(__file__).parent / "commit_registry_client.py"
                    if _p.exists():
                        _spec = _util.spec_from_file_location("commit_registry_client", str(_p))
                        if _spec and _spec.loader:
                            _m = _util.module_from_spec(_spec)
                            _sys.modules["commit_registry_client"] = _m
                            _spec.loader.exec_module(_m)
                            get_client = getattr(_m, "get_client", None)
                except Exception:
                    get_client = None
        if get_client is None:
            logger.warning("commit_observer_client_unavailable")
            return []
        client = get_client()

    if len(shas) == 1:
        try:
            client.register(
                sha=shas[0],
                role=session_role,
                pipeline_id=pipeline_id,
                repo=repo,
                branch=branch,
            )
        except Exception as exc:  # pragma: no cover - client already swallows
            logger.warning(
                "commit_observer_register_failed",
                sha=shas[0],
                role=session_role,
                error=str(exc),
            )
        return shas

    items = [
        {
            "sha": sha,
            "role": session_role,
            "pipeline_id": pipeline_id,
            "repo": repo,
            "branch": branch,
        }
        for sha in shas
    ]
    try:
        client.register_bulk(items)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "commit_observer_register_bulk_failed",
            count=len(items),
            role=session_role,
            error=str(exc),
        )
    return shas


def observe_after_git_execute(
    exec_path: str,
    *,
    before_head: str | None,
    branch: str | None,
    session_role: str | None,
    pipeline_id: str | None,
    repo: str | None,
    registry_client: Any | None = None,
) -> Iterable[str]:
    """Convenience wrapper that captures ``after_head`` itself.

    Used by ``git_execute`` — the handler only captures ``before_head``
    explicitly; ``after_head`` is freshly sampled.  Always returns an
    iterable (possibly empty) for a simpler call site.
    """
    if not session_role:
        return []
    after_head = capture_head(exec_path)
    if after_head is None:
        return []
    return observe(
        exec_path,
        before_head=before_head,
        after_head=after_head,
        branch=branch,
        session_role=session_role,
        pipeline_id=pipeline_id,
        repo=repo,
        registry_client=registry_client,
    )
