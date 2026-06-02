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
from collections.abc import Callable, Iterable
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


def _build_argv(git_cmd: Callable[..., list[str]] | None, *args: str) -> list[str]:
    """Build a ``git`` argv, honouring an optional gateway-style prefix builder.

    ``git_cmd`` lets callers (notably ``gateway.git_client``) inject the
    ``-c safe.directory=* -c core.hooksPath=/dev/null -c gc.auto=0`` prefix
    they use everywhere else; the observer's own callers pass ``None`` and
    invoke plain ``git``.
    """
    return git_cmd(*args) if git_cmd else ["git", *args]


def patch_id_for_commit(
    exec_path: str,
    sha: str,
    *,
    git_cmd: Callable[..., list[str]] | None = None,
) -> str | None:
    """Return ``git patch-id --stable`` for ``sha`` or ``None``.

    Recorded at registration time so attribution survives a later SHA
    rewrite (rebase): the rewritten commit keeps the same patch-id even
    though its SHA changes (#2932).  We feed ``git show`` through
    ``git patch-id --stable`` — the latter reads only the diff hunks, so
    the commit header is ignored and root commits work.  Merge commits
    (``git show`` emits no diff by default), empty commits, and
    rename/mode-only commits yield no patch-id; we return ``None`` and the
    commit simply doesn't get content-based recovery.  Any failure returns
    ``None`` so registration proceeds with SHA-only attribution.

    ``git_cmd`` is an optional argv builder so the gateway's push-time
    recovery path can pass its hardened ``safe.directory`` / hooks-path /
    gc settings.  The observer's own callers pass ``None``.
    """
    sha_s = (sha or "").strip()
    if not sha_s:
        return None
    try:
        show = subprocess.run(
            _build_argv(git_cmd, "show", "--no-color", sha_s),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if show.returncode != 0:
            return None
        patch = subprocess.run(
            _build_argv(git_cmd, "patch-id", "--stable"),
            cwd=exec_path,
            input=show.stdout,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        logger.debug("commit_observer_patch_id_failed", sha=sha_s, error=str(exc))
        return None
    if patch.returncode != 0:
        return None
    # ``git patch-id`` prints ``<patch-id> <commit-id>``; take the first
    # token.  Empty output (no diff) means no usable patch-id.
    parts = (patch.stdout or "").split()
    return parts[0] if parts else None


def patch_ids_for_commits(
    exec_path: str,
    shas: Iterable[str],
    *,
    git_cmd: Callable[..., list[str]] | None = None,
) -> dict[str, str | None]:
    """Bulk version of :func:`patch_id_for_commit` keyed by SHA.

    Replaces N pairs of ``git show | git patch-id`` subprocesses with one
    ``git log --no-walk -p ... | git patch-id --stable``, so a rebase
    recovery that touches dozens of commits doesn't pay N × 2 spawn
    overhead serially.

    The return dict carries an entry for every input SHA: a string when
    git emitted a patch-id for it, or ``None`` for SHAs ``git log`` did
    not output (merge / empty / rename-only commits, or shas the git
    invocation could not resolve).  Any subprocess failure returns a dict
    with every input SHA mapped to ``None`` — the caller (push handler)
    treats that as "no content-based recovery available" and leaves the
    commits fail-closed.
    """
    sha_list: list[str] = []
    seen: set[str] = set()
    for raw in shas:
        s = (raw or "").strip()
        if s and s not in seen:
            seen.add(s)
            sha_list.append(s)
    result: dict[str, str | None] = dict.fromkeys(sha_list)
    if not sha_list:
        return result

    try:
        show = subprocess.run(
            _build_argv(git_cmd, "log", "--no-walk", "-p", "--no-color", *sha_list),
            cwd=exec_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if show.returncode != 0 or not show.stdout:
            return result
        patch = subprocess.run(
            _build_argv(git_cmd, "patch-id", "--stable"),
            cwd=exec_path,
            input=show.stdout,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        logger.debug(
            "commit_observer_patch_ids_failed",
            count=len(sha_list),
            error=str(exc),
        )
        return result
    if patch.returncode != 0:
        return result

    # ``git patch-id --stable`` prints one ``<patch-id> <commit-id>`` pair
    # per input patch.  Map back to the SHA list so callers can correlate.
    valid = set(sha_list)
    for line in (patch.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        pid, commit_sha = parts[0], parts[1]
        if commit_sha in valid:
            result[commit_sha] = pid
    return result


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
                patch_id=patch_id_for_commit(exec_path, shas[0]),
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
            "patch_id": patch_id_for_commit(exec_path, sha),
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
