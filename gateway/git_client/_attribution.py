"""Per-file push attribution (commit -> author role).

Extracted verbatim from the pre-split ``gateway/git_client.py``
(#3312 slice-11). AST-identical to the originals — pure refactor.
"""

from typing import Any

from egg_logging import get_logger

from ._push_analysis import (
    _SHA_LINE_RE,
    _fallback_base_candidates,
    _fetch_base_branch_best_effort,
    _parse_sha_lines,
)
from ._remote import git_cmd

logger = get_logger("gateway.git-client")


# ---------------------------------------------------------------------------
# Per-commit attribution for the gateway auto-filter (issue #1882)
# ---------------------------------------------------------------------------


from dataclasses import dataclass, field  # noqa: E402 - colocated with the API it supports


@dataclass
class AttributedFile:
    """One file in a push, tagged with the commit that introduced it and its author role.

    ``authored_by`` is the role string the commit-authorship registry
    returned for ``commit_sha`` (e.g. ``"coder"``), or ``None`` when the
    commit is unregistered.  The push handler treats ``None`` as
    fail-closed (own-authored for the pushing role's restriction check).
    """

    path: str
    commit_sha: str
    authored_by: str | None = None


@dataclass
class AttributedPushRange:
    """Result of ``get_attributed_changed_files_in_push``.

    Bundles the per-file attribution with the ordered list of commits
    that produced the range and the full set of SHAs we handed to the
    registry lookup.  The push handler consumes:

    - ``files``:                per-file attribution
    - ``commits``:              oldest-first SHA list (topological order)
    - ``attribution``:          raw sha → role map returned by the registry
    - ``error``:                non-None fail-closed message when the
                                underlying diff detection failed.
    """

    files: list[AttributedFile] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    attribution: dict[str, str | None] = field(default_factory=dict)
    error: str | None = None


def _enumerate_push_commits(
    repo_path: str, remote: str, branch: str, base_branch: str | None = None
) -> tuple[list[str], str | None]:
    """Return (commits_oldest_first, error).

    Mirrors ``get_changed_files_in_push`` rev-list logic: prefers
    ``<remote>/<branch>..HEAD``, then falls back to merge-base with the
    pipeline's ``base_branch`` (then main/master) for new-branch pushes.
    Diffing against ``base_branch`` first keeps commits inherited from a
    non-trunk base out of the range so they are not attributed to the
    pushing role (#3024).  On any error, returns ``([], "...")`` — the
    caller then fails closed.  Output lines that don't parse as a git SHA
    (7–64 lowercase hex) are rejected so that a misbehaving git wrapper
    can't smuggle arbitrary strings into the commit list.
    """
    import subprocess

    # Best-effort fetch so origin/<branch> is up to date.
    try:
        subprocess.run(
            git_cmd("fetch", remote, branch),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        pass

    def _rev_list(base: str) -> list[str] | None:
        result = subprocess.run(
            git_cmd("rev-list", "--reverse", f"{base}..HEAD"),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return None
        return _parse_sha_lines(result.stdout)

    primary = _rev_list(f"{remote}/{branch}")
    if primary is not None:
        return primary, None

    # New-branch fallback: prefer the pipeline's configured base_branch so
    # commits inherited from a non-trunk base stay out of the range (#3024).
    # The helper short-circuits if the ref is already local (the sister
    # function in the same push already fetched it), avoiding a redundant
    # network round-trip.
    if base_branch and base_branch != "HEAD":
        _fetch_base_branch_best_effort(repo_path, remote, base_branch)
    for default_branch in _fallback_base_candidates(base_branch):
        mb = subprocess.run(
            git_cmd("merge-base", f"{remote}/{default_branch}", "HEAD"),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if mb.returncode != 0:
            continue
        fork_point = (mb.stdout or "").strip()
        if not _SHA_LINE_RE.match(fork_point):
            continue
        rl = _rev_list(fork_point)
        if rl is not None:
            return rl, None

    return [], "Could not determine push commit range - push blocked for security"


def _files_for_commit(repo_path: str, sha: str) -> tuple[list[str], str | None]:
    """diff-tree one commit; returns (files, error)."""
    import subprocess

    result = subprocess.run(
        git_cmd("diff-tree", "--no-commit-id", "--name-only", "-r", sha),
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return [], (
            f"diff-tree failed for {sha}: "
            f"rc={result.returncode} stderr={(result.stderr or '').strip()}"
        )
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()], None


def _patch_ids_for_commits(repo_path: str, shas: list[str]) -> dict[str, str | None]:
    """Bulk ``git patch-id --stable`` for ``shas`` via the gateway's hardened argv.

    Push-time recovery helper for #2932: a rebase rewrites SHAs but the
    patch-id is content-stable, so the original author is recoverable.
    Delegates to ``commit_observer.patch_ids_for_commits`` (registration
    side) so the two sides share one implementation; we just inject the
    gateway's ``git_cmd`` argv builder so the safe-directory / hooks-path
    / gc settings still apply.

    Returns a dict keyed by every input SHA — string when git emitted a
    patch-id, ``None`` for merge / empty / rename-only commits or any git
    failure (the commit then stays fail-closed in the caller).
    """
    try:
        from ..commit_observer import patch_ids_for_commits
    except ImportError:
        from commit_observer import (  # type: ignore[no-redef,import-untyped]
            patch_ids_for_commits,
        )
    return patch_ids_for_commits(repo_path, shas, git_cmd=git_cmd)


# Reserved attribution role for commits created by egg infrastructure
# (the orchestrator state-file committer, the salvage helper, and the
# auto-formatter — which rides on the orchestrator's identity rather than
# a distinct git config) rather than by an agent session.  It is deliberately
# a string no agent role can ever equal, so the push handler classifies these
# commits as pulled-from-other-role (never own-authored) and never blocks a
# producer for files an infra commit touched.  See #2927.
INFRA_ATTRIBUTION_ROLE = "infra"


# Committer emails used exclusively by egg infrastructure.  An *agent* commit
# carries ``{role}@egg.local`` only when ``EGG_AGENT_ROLE`` is set in the
# sandbox (see sandbox/entrypoint.py); a role-less sandbox would fall back to
# ``egg@localhost`` and collide with this allowlist.  The invariant that keeps
# the exemption safe is the **orchestrator-gateway pairing**: the orchestrator
# always injects ``EGG_AGENT_ROLE`` *and* opens the gateway session with
# matching ``agent_role`` metadata.  The push handler's restriction check at
# gateway.py only fires when the session's ``agent_role`` is set, so any path
# that wired up an agent session without ``EGG_AGENT_ROLE`` would also skip
# the restriction logic entirely.  An operator who overrides the gateway's
# ``EGG_USER_GIT_EMAIL`` only loses the exemption (the push fails closed as
# before) — never gains a bypass, so the allowlist is the safe failure
# direction.  Sources:
#   - egg@localhost          orchestrator/entrypoint.sh (also used by the
#                            auto-formatter, which runs in the orchestrator's
#                            pre-commit chain rather than under a distinct
#                            identity)
#   - egg@example.com        gateway/entrypoint.sh default
#   - egg-salvage@localhost  orchestrator/agent_salvage.py
INFRA_COMMITTER_EMAILS: frozenset[str] = frozenset(
    {
        "egg@localhost",
        "egg@example.com",
        "egg-salvage@localhost",
    }
)


def _committer_email_for_commit(repo_path: str, sha: str) -> str | None:
    """Return the committer email for ``sha`` (lower-cased) or ``None``.

    Used to recognise infra-authored commits the authorship registry never
    saw.  Reads the *committer* (not author) identity because rebases and
    cherry-picks preserve the original author while the committer reflects
    who actually produced the SHA on this branch.  Any failure returns
    ``None`` so the caller falls back to fail-closed (own-authored).
    """
    import subprocess

    try:
        result = subprocess.run(
            git_cmd("show", "-s", "--format=%ce", sha),
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    email = (result.stdout or "").strip().lower()
    return email or None


def get_attributed_changed_files_in_push(
    repo_path: str,
    remote: str,
    branch: str,
    session_role: str | None = None,
    registry_client: object | None = None,
    base_branch: str | None = None,
) -> AttributedPushRange:
    """Return attributed files + commit range for a planned push.

    ``session_role`` is advisory (currently used only for audit logging
    in the caller); the function itself does not filter on it.

    ``base_branch`` is forwarded to ``_enumerate_push_commits`` as the
    preferred new-branch diff base so commits inherited from a non-trunk
    base are excluded from the attributed range (#3024).

    The caller supplies a ``registry_client`` with a ``lookup_bulk``
    method so the gateway's push handler can mock the client in tests
    and avoid the network round-trip when it already knows the
    attribution (e.g. for internal pushes).  When ``None`` we
    lazily import the module-level client.

    On any detection failure, returns an ``AttributedPushRange`` with
    ``error`` set and ``files`` empty — the caller MUST fail closed.
    """
    commits, err = _enumerate_push_commits(repo_path, remote, branch, base_branch=base_branch)
    if err:
        logger.error(
            "get_attributed_changed_files_in_push enumeration failed — failing closed",
            repo_path=repo_path,
            remote=remote,
            branch=branch,
            error=err,
            session_role=session_role,
        )
        return AttributedPushRange(error=err)

    files: list[AttributedFile] = []
    for sha in commits:
        file_list, file_err = _files_for_commit(repo_path, sha)
        if file_err:
            logger.error(
                "get_attributed_changed_files_in_push diff-tree failed — failing closed",
                repo_path=repo_path,
                remote=remote,
                branch=branch,
                sha=sha,
                error=file_err,
                session_role=session_role,
            )
            return AttributedPushRange(error=file_err, commits=commits)
        for path in file_list:
            files.append(AttributedFile(path=path, commit_sha=sha))

    # Bulk-lookup every distinct SHA in the range, then tag each file.
    attribution: dict[str, str | None] = {}
    if commits:
        if registry_client is None:
            # Resolve ``get_client`` from the sibling commit_registry_client
            # module.  The conftest used by gateway tests does not preload
            # that module, so we fall back to an explicit file-path load.
            import sys as _sys

            _crc_mod = _sys.modules.get("commit_registry_client") or _sys.modules.get(
                "gateway.commit_registry_client"
            )
            get_client = getattr(_crc_mod, "get_client", None) if _crc_mod else None
            if get_client is None:
                try:
                    # The primary path finds the module preloaded; this
                    # fallback only fires for standalone runners.
                    import commit_registry_client as _crc  # type: ignore[import-untyped]

                    get_client = _crc.get_client
                except ImportError:
                    try:
                        import importlib.util as _util
                        from pathlib import Path as _Path

                        _p = _Path(__file__).parent.parent / "commit_registry_client.py"
                        if _p.exists():
                            _spec = _util.spec_from_file_location("commit_registry_client", str(_p))
                            if _spec and _spec.loader:
                                _m = _util.module_from_spec(_spec)
                                _sys.modules["commit_registry_client"] = _m
                                _spec.loader.exec_module(_m)
                                get_client = getattr(_m, "get_client", None)
                    except Exception:
                        get_client = None
            if get_client is not None:
                registry_client = get_client()
        if registry_client is not None:
            try:
                # registry_client is typed as ``object`` to avoid a hard
                # import dependency on commit_registry_client.  The
                # method is exercised by callers via duck-typing; cast
                # to Any so mypy accepts the attribute access whether
                # or not the stub is reachable.
                _rc_any: Any = registry_client
                attribution = dict(_rc_any.lookup_bulk(list(commits)))
            except Exception:
                logger.warning(
                    "commit_authorship_lookup_exception",
                    repo_path=repo_path,
                    branch=branch,
                    exc_info=True,
                )
                attribution = {}

    # Distinguish full-coverage lookup from partial coverage.  Missing
    # SHAs fall through to fail-closed (``None`` → own-authored) below,
    # so a flaky registry would silently subject every cross-role push
    # to restriction checks without any operator-visible signal.  Log
    # partial responses at WARNING with counts so operators can spot
    # the drift.
    if commits:
        requested_shas = set(commits)
        received_shas = {sha for sha in attribution if sha in requested_shas}
        if received_shas and received_shas != requested_shas:
            logger.warning(
                "commit_authorship_partial_lookup",
                repo_path=repo_path,
                branch=branch,
                requested=len(requested_shas),
                received=len(received_shas),
                missing=len(requested_shas - received_shas),
            )

    # #2932: rescue commits whose SHA the registry never saw because a
    # rebase rewrote them.  The patch-id is stable across the SHA rewrite,
    # so for any commit still unattributed we compute its patch-id and ask
    # the registry which role authored a commit with that patch-id.  Only a
    # byte-identical diff matches, and an ambiguous patch-id resolves to
    # ``None`` (registry-side), so this preserves #2039's fail-closed
    # intent: it can only recover an attribution, never fabricate a bypass.
    if commits and registry_client is not None:
        lookup_patch_ids = getattr(registry_client, "lookup_patch_ids", None)
        if callable(lookup_patch_ids):
            unattributed = [sha for sha in commits if attribution.get(sha) is None]
            sha_to_patch: dict[str, str] = {}
            if unattributed:
                # One batched ``git log --no-walk -p | git patch-id`` rather
                # than N pairs of subprocess spawns — a recovery rebase that
                # touches dozens of commits stays linear in git, not in
                # Python process overhead.
                for sha, pid in _patch_ids_for_commits(repo_path, unattributed).items():
                    if pid:
                        sha_to_patch[sha] = pid
            if sha_to_patch:
                try:
                    patch_attr = dict(lookup_patch_ids(sorted(set(sha_to_patch.values()))))
                except Exception:
                    logger.warning(
                        "commit_authorship_patch_lookup_exception",
                        repo_path=repo_path,
                        branch=branch,
                        exc_info=True,
                    )
                    patch_attr = {}
                recovered: list[str] = []
                for sha, pid in sha_to_patch.items():
                    role = patch_attr.get(pid)
                    if role:
                        attribution[sha] = role
                        recovered.append(sha)
                if recovered:
                    logger.info(
                        "commit_authorship_patch_id_recovery",
                        repo_path=repo_path,
                        branch=branch,
                        session_role=session_role,
                        recovered=len(recovered),
                        shas=recovered,
                    )

    # #2927: rescue commits the registry never saw because they were created
    # by egg infrastructure (orchestrator state-file commits, auto-formatter,
    # salvage) outside any agent session.  Without this, such commits stay
    # ``None`` and the push handler treats them as own-authored (fail-closed),
    # wedging a producer whose branch merely inherited them.  We only relax
    # for committers in the trusted infra allowlist; unknown unregistered
    # authors remain ``None`` and continue to fail closed.  Runs after the
    # #2932 patch-id rescue so a rebased *agent* commit is still attributed
    # to its original role rather than being reclassified as infra.
    infra_exempted: list[str] = []
    for sha in commits:
        if attribution.get(sha) is not None:
            continue
        email = _committer_email_for_commit(repo_path, sha)
        if email is not None and email in INFRA_COMMITTER_EMAILS:
            attribution[sha] = INFRA_ATTRIBUTION_ROLE
            infra_exempted.append(sha)
    if infra_exempted:
        logger.info(
            "commit_authorship_infra_exemption",
            repo_path=repo_path,
            branch=branch,
            session_role=session_role,
            exempted=len(infra_exempted),
            shas=infra_exempted,
        )

    for f in files:
        f.authored_by = attribution.get(f.commit_sha)

    return AttributedPushRange(
        files=files,
        commits=commits,
        attribution=attribution,
    )
