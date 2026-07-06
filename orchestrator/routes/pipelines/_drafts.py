"""Drafts helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pre-split barrel. Patched barrel globals are
reached through ``import routes.pipelines as _pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import subprocess  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

import routes.pipelines as _pkg  # noqa: E402,F401


def _verdict_path_for_type(
    phase: str,
    reviewer_type: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str:
    """Return the relative verdict file path for a given reviewer type.

    Uses issue_number as prefix when available, otherwise pipeline_id.
    """
    prefix = _pkg._pipeline_identifier(issue_number, pipeline_id or "unknown")
    return f".egg-state/reviews/{prefix}-{phase}-{reviewer_type}-review.json"


def _draft_filename(phase: str) -> str | None:
    """Return the draft filename for a phase, without any prefix.

    Centralises the phase-to-filename mapping so that
    ``_get_draft_path`` and ``_get_generic_draft_path`` stay in sync.
    """
    if phase == "refine":
        return "analysis.md"
    elif phase == "implement":
        return None
    else:
        return f"{phase}.md"


def _get_draft_path(
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Return relative path to the draft file for a phase.

    Spec-driven (#3077 slice-3): the registered ``refine`` and ``plan``
    phases route through :func:`egg_contracts.artifact_spec.resolve_artifact_path`
    so the registry is the single source of truth that propose-time
    validation (:func:`orchestrator.routes.signals._validate_producer_artifacts`)
    and every draft reader in this module share. Slice-2 of #3077 pins
    the equality with a mandatory consistency test
    (``TestConsistencyB_GetDraftPathEquality`` in
    ``shared/egg_contracts/tests/test_artifact_spec.py``); the slice-3
    rewrite below makes that equality structural rather than incidental
    — refine-risk-1's "no second copy of path knowledge" ratchet.

    Phases not yet registered in the spec (currently ``pr``) keep their
    legacy path via the centralised ``_draft_filename`` mapping, so
    pre-existing PR-phase callers stay byte-identical. ``implement``
    has no draft and falls out as ``None`` here.

    Uses ``issue_number`` as prefix when available, otherwise
    ``pipeline_id``; falls back to ``"unknown"`` when neither is supplied.
    """
    _SPEC_BY_PHASE = {"refine": "analysis-draft", "plan": "plan-draft"}
    spec_name = _SPEC_BY_PHASE.get(phase)
    if spec_name is not None:
        # Lazy import: the spec module is pure Python and has no
        # orchestrator/gateway deps, but importing it at module load
        # time would still pull egg_contracts into pipelines.py's
        # import graph regardless of whether _get_draft_path is called
        # — keep the deferral so the import cost only lands on actual
        # invocations.
        from egg_contracts.artifact_spec import resolve_artifact_path

        identifier = _pkg._pipeline_identifier(issue_number, pipeline_id or "unknown")
        return resolve_artifact_path(spec_name, identifier)

    filename = _draft_filename(phase)
    if not filename:
        return None
    prefix = _pkg._pipeline_identifier(issue_number, pipeline_id or "unknown")
    return f".egg-state/drafts/{prefix}-{filename}"


_HUMAN_SPEC_BY_PHASE = {"refine": "analysis-draft-human", "plan": "plan-draft-human"}


def _get_human_draft_path(
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
) -> str | None:
    """Return the relative path to the human-focused companion draft.

    Returns ``None`` for phases without a registered human companion
    (currently only ``refine`` and ``plan`` have one).
    """
    spec_name = _HUMAN_SPEC_BY_PHASE.get(phase)
    if spec_name is None:
        return None
    from egg_contracts.artifact_spec import resolve_artifact_path

    identifier = _pkg._pipeline_identifier(issue_number, pipeline_id or "unknown")
    return resolve_artifact_path(spec_name, identifier)


def _cleanup_stale_generic_drafts(worktree_path: Path) -> bool:
    """Remove unprefixed generic draft files from a worktree.

    Legacy pipelines left behind ``analysis.md`` and ``plan.md`` (without
    an issue-number or pipeline-id prefix) in ``.egg-state/drafts/``.
    These stale files can confuse downstream draft-reading logic.  This
    helper deletes only the exact unprefixed filenames; prefixed files
    (e.g. ``1553-analysis.md``) are left untouched.

    Uses ``git rm`` so the deletions are staged and can be committed
    immediately.  Falls back to ``os.unlink`` if the file is untracked.

    Safe to call when the drafts directory does not exist (no-op).

    Returns ``True`` if a commit was made (i.e. tracked files were removed
    and committed), ``False`` otherwise.
    """
    drafts_dir = worktree_path / ".egg-state" / "drafts"
    if not drafts_dir.is_dir():
        return False

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
    ]
    removed = False

    stale_names = ("analysis.md", "plan.md")
    for name in stale_names:
        stale = drafts_dir / name
        if stale.exists():
            _pkg.logger.info(
                "Removing stale generic draft",
                path=str(stale),
            )
            try:
                subprocess.run(
                    [*git_base, "rm", "-f", str(stale.relative_to(worktree_path))],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                removed = True
            except subprocess.CalledProcessError as exc:
                # File may be untracked — just delete it from disk.
                # Warn so that unexpected git rm failures (e.g. index
                # lock) are diagnosable.
                _pkg.logger.warning(
                    "git rm failed for stale draft, falling back to unlink",
                    path=str(stale),
                    error=str(exc),
                )
                stale.unlink(missing_ok=True)

    if removed:
        try:
            subprocess.run(
                [
                    *git_base,
                    "commit",
                    "--no-verify",
                    "-m",
                    "Remove stale generic draft files",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return True
        except subprocess.CalledProcessError as commit_err:
            _pkg.logger.debug(
                "No changes to commit after stale draft cleanup",
                error=str(commit_err),
            )

    return False


def _get_generic_draft_path(phase: str) -> str | None:
    """Return the generic (unprefixed) draft path for a phase.

    Used as a fallback when the issue-specific draft file is missing.
    """
    filename = _draft_filename(phase)
    if not filename:
        return None
    return f".egg-state/drafts/{filename}"


def _git_show_draft(
    repo_path: Path,
    branch: str,
    rel_path: str,
    timeout: int = 15,
) -> str | None:
    """Read a file from ``origin/{branch}`` via ``git show``.

    Returns the file content as a string, or ``None`` if the file does
    not exist on the remote ref or the git command fails.  This is a
    read-only operation that does not modify the worktree.

    Note: this function does **not** ``git fetch`` itself.  The caller is
    responsible for ensuring ``origin/{branch}`` is fresh (e.g., by
    running ``git fetch origin {branch}`` before calling this helper).
    """
    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={repo_path}",
        "-C",
        str(repo_path),
    ]
    try:
        result = subprocess.run(
            [*git_base, "show", f"origin/{branch}:{rel_path}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        if result.returncode != 0:
            _pkg.logger.debug(
                "git show returned non-zero",
                branch=branch,
                rel_path=rel_path,
                returncode=result.returncode,
                stderr=result.stderr.strip()[:200],
            )
    except Exception as exc:
        _pkg.logger.debug(
            "git show failed for draft",
            branch=branch,
            rel_path=rel_path,
            error=str(exc),
        )
    return None


def _read_source_branch_artifacts(
    repo_path: Path,
    source_branch: str,
    issue_number: int | None,
    pipeline_id: str,
    store: Any,
    pipeline: Any,
    source_artifact_prefix: str | None = None,
    spawner: Any | None = None,
    gateway_mode: str = "public",
) -> bool:
    """Read plan and analysis artifacts from a source branch.

    Reads draft files from ``origin/<source_branch>`` via ``git show``.
    Only populates ``pipeline.plan`` and ``pipeline.analysis`` when they
    are not already set (inline values take precedence).

    Prefix resolution order for the exact-path lookup:

    1. ``source_artifact_prefix`` (explicit override, e.g. ``"issue-1570-v3"``)
    2. ``pipeline_id`` (includes qualifier, e.g. ``"issue-1570-v7"``)
    3. ``issue_number`` (bare issue number, e.g. ``1570``)

    Falls back to listing available files via ``git ls-tree`` when none
    of the prefixes match.

    Args:
        repo_path: Path to the repository (worktree or main).
        source_branch: Branch name to read artifacts from.
        issue_number: Pipeline issue number (for deriving prefix).
        pipeline_id: Pipeline ID (includes qualifier when present).
        store: StateStore instance for saving updated pipeline.
        pipeline: Pipeline model instance to populate.
        source_artifact_prefix: Explicit prefix override for draft
            filenames on the source branch (e.g. ``"issue-1570-v3"``).
            When set, only this prefix is tried before the ls-tree
            fallback.
        spawner: ContainerSpawner instance for gateway-authenticated git
            operations.  When provided, the fetch uses the gateway API
            (which injects GitHub credentials) instead of a raw
            ``git fetch`` that lacks auth in the sandboxed environment.
        gateway_mode: Network mode for the gateway session (``"public"``
            or ``"private"``).

    Returns:
        True if any artifacts were read, False otherwise.
    """
    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={repo_path}",
        "-C",
        str(repo_path),
    ]
    # Bare prefix is the issue number when available — used as a fallback
    # after the full pipeline_id prefix.  Do NOT use _pipeline_identifier()
    # here because it returns pipeline_id for qualifier-tagged pipelines,
    # which defeats the fallback chain (pipeline_id → bare issue number).
    bare_prefix: int | str = issue_number if issue_number is not None else pipeline_id
    updated = False

    # Fetch the source branch so origin/{source_branch} is up-to-date.
    # Without this, git show fails because the remote ref isn't cached
    # locally.  Use the gateway-authenticated fetch when available —
    # raw git commands in the sandboxed environment lack GitHub
    # credentials (the gateway sidecar injects them).
    if spawner is not None:
        try:
            spawner.gateway.fetch_branch(
                pipeline_id=pipeline_id,
                repo_path=str(repo_path),
                args=[source_branch],
                mode=gateway_mode,
            )
        except Exception:
            _pkg.logger.warning(
                "Gateway fetch of source branch failed (will try git show anyway)",
                source_branch=source_branch,
                pipeline_id=pipeline_id,
                exc_info=True,
            )
    else:
        # Fallback for tests or environments without a gateway.
        try:
            subprocess.run(
                [*git_base, "fetch", "origin", source_branch],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            _pkg.logger.debug(
                "Failed to fetch source branch (will try git show anyway)",
                source_branch=source_branch,
                exc_info=True,
            )

    # Build ordered list of prefixes to try.  Duplicates are removed so
    # we don't hit git show twice for the same path.
    if source_artifact_prefix is not None:
        # Explicit override — try only this prefix before ls-tree fallback.
        prefixes: list[str | int] = [source_artifact_prefix]
    else:
        # Default: try pipeline_id first (includes qualifier), then bare
        # issue number.  When pipeline_id == bare_prefix (e.g. no qualifier
        # and no issue number), the dedup below collapses them.
        prefixes = []
        if pipeline_id and str(pipeline_id) != str(bare_prefix):
            prefixes.append(pipeline_id)
        prefixes.append(bare_prefix)

    for field_name, suffix in [("analysis", "-analysis.md"), ("plan", "-plan.md")]:
        # Skip if already populated (inline values take precedence).
        # Use ``is not None`` so empty strings are not silently overwritten.
        if getattr(pipeline, field_name) is not None:
            continue

        drafts_prefix = ".egg-state/drafts/"
        content = None

        # Try each prefix in order (exact path lookup).
        for pfx in prefixes:
            expected_path = f"{drafts_prefix}{pfx}{suffix}"
            content = _pkg._git_show_draft(repo_path, source_branch, expected_path)
            if content:
                _pkg.logger.info(
                    "Read artifact from source branch (exact prefix)",
                    field=field_name,
                    source_branch=source_branch,
                    path=expected_path,
                )
                break

        if content is None:
            # Fallback: list available files and find a match
            try:
                result = subprocess.run(
                    [
                        *git_base,
                        "ls-tree",
                        "--name-only",
                        f"origin/{source_branch}:{drafts_prefix.rstrip('/')}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    matches = [f for f in result.stdout.strip().splitlines() if f.endswith(suffix)]
                    # Filter by issue number to avoid picking up artifacts
                    # from other issues on the same branch (#1654).
                    if issue_number is not None:
                        issue_matches = [f for f in matches if f.startswith(f"{issue_number}-")]
                        if issue_matches:
                            matches = issue_matches
                        else:
                            _pkg.logger.warning(
                                "No fallback match for issue number — skipping",
                                field=field_name,
                                issue_number=issue_number,
                                source_branch=source_branch,
                                available=matches,
                            )
                            continue
                    if len(matches) > 1:
                        _pkg.logger.warning(
                            "Multiple fallback matches for artifact — using first",
                            field=field_name,
                            source_branch=source_branch,
                            matches=matches,
                        )
                    for filename in matches:
                        fallback_path = f"{drafts_prefix}{filename}"
                        content = _pkg._git_show_draft(repo_path, source_branch, fallback_path)
                        if content:
                            _pkg.logger.info(
                                "Read artifact from source branch via fallback",
                                field=field_name,
                                source_branch=source_branch,
                                path=fallback_path,
                            )
                            break
            except Exception as exc:
                _pkg.logger.debug(
                    "git ls-tree failed for source branch drafts",
                    source_branch=source_branch,
                    error=str(exc),
                )

        if content:
            setattr(pipeline, field_name, content)
            updated = True
            _pkg.logger.info(
                "Read artifact from source branch",
                field=field_name,
                source_branch=source_branch,
                pipeline_id=pipeline_id,
                length=len(content),
            )

    if updated:
        # Clear source_branch after successful read to avoid re-reading on
        # pipeline restart (same pattern as plan/analysis clearing after
        # draft files are pushed).
        pipeline.source_branch = None
        pipeline.source_artifact_prefix = None
        store.save_pipeline(
            pipeline, message=f"Populate artifacts from source branch {source_branch}"
        )
    else:
        _pkg.logger.warning(
            "No artifacts found on source branch",
            source_branch=source_branch,
            pipeline_id=pipeline_id,
            source_artifact_prefix=source_artifact_prefix,
        )

    return updated


def _pull_contract_from_source_branch(
    repo_path: Path,
    source_branch: str,
    issue_number: int | None,
    pipeline_id: str,
    spawner: Any | None = None,
    gateway_mode: str = "public",
    task_description: str | None = None,
) -> bool:
    """Load a persisted contract from ``origin/<source_branch>`` into the worktree.

    When ``submit_task`` is called with ``source_branch``, the source branch
    carries ``.egg-state/contracts/<pipeline>.json`` (with any resolved HITL
    decisions).  Without this helper, ``_run_pipeline`` calls
    ``create_contract()`` unconditionally and overwrites those decisions with
    a zero-state contract (#2035).  This helper fetches the source branch,
    reads the contract via ``git show``, rebinds its pipeline_id to the new
    pipeline, and writes it into the worktree so the caller can skip
    ``create_contract()`` and proceed to commit+push the pulled contract.

    ``task_description`` is the NEW submit's composed task statement
    (``compose_task_description`` at the call site — identity anchor +
    resubmit prompt, #3163). The pulled contract carries the SOURCE
    pipeline's ``task_description``, but the new submit's statement is
    authoritative for THIS pipeline and is where operators put binding
    resume directives (e.g. "adopt prior branch X, do not reimplement"
    — #3123). When non-empty it replaces the pulled value; the source
    value stays recoverable from the source branch's git history. This
    replacement is also what keeps a fork from leaking the source
    pipeline's task into the new pipeline's per-event prompts: issue
    and JIRA pipelines always compose a non-empty anchor, so the pulled
    cross-pipeline text never survives. Only a free-text resume with a
    blank prompt preserves the pulled value (a plain resume of the same
    task).

    Returns True when a contract was successfully pulled, False otherwise.
    Best-effort: missing, invalid, or unreachable source contracts all yield
    False so the caller falls back to ``create_contract()``.
    """
    from egg_contracts.loader import (
        ContractNotFoundError,
        ContractValidationError,
        load_contract_from_branch,
        save_contract,
    )

    # Fetch the source branch so origin/<source_branch> is current.  Mirrors
    # the pattern in _read_source_branch_artifacts — use the gateway when
    # available, fall back to raw git for tests / non-sandboxed callers.
    if spawner is not None:
        try:
            spawner.gateway.fetch_branch(
                pipeline_id=pipeline_id,
                repo_path=str(repo_path),
                args=[source_branch],
                mode=gateway_mode,
            )
        except Exception:
            _pkg.logger.warning(
                "Gateway fetch of source branch failed (will try git show anyway)",
                source_branch=source_branch,
                pipeline_id=pipeline_id,
                exc_info=True,
            )
    else:
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    f"safe.directory={repo_path}",
                    "-C",
                    str(repo_path),
                    "fetch",
                    "origin",
                    source_branch,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            _pkg.logger.debug(
                "Failed to fetch source branch for contract pull",
                source_branch=source_branch,
                exc_info=True,
            )

    identifier: int | str = issue_number if issue_number is not None else pipeline_id

    try:
        contract = load_contract_from_branch(
            identifier,
            repo_path,
            branch=f"origin/{source_branch}",
        )
    except ContractNotFoundError:
        _pkg.logger.debug(
            "No contract on source branch",
            pipeline_id=pipeline_id,
            source_branch=source_branch,
        )
        return False
    except ContractValidationError as e:
        _pkg.logger.warning(
            "Contract on source branch failed validation, falling back to fresh contract",
            pipeline_id=pipeline_id,
            source_branch=source_branch,
            error=str(e),
        )
        return False
    except Exception:
        _pkg.logger.warning(
            "Failed to load contract from source branch",
            pipeline_id=pipeline_id,
            source_branch=source_branch,
            exc_info=True,
        )
        return False

    # Rebind to the new pipeline_id so save_contract writes under the new
    # canonical key when the pipeline was forked with a qualifier
    # (e.g. source=issue-1965, new=issue-1965-v2).
    contract.pipeline_id = pipeline_id
    # Refresh the task statement from the new submit (#3123/#3163):
    # without this, the resubmit's composed statement — identity anchor
    # plus any operator resume directives — never reaches any
    # agent-visible surface, because the caller skips create_contract()
    # (the only other writer of ``task_description``) whenever the pull
    # succeeds. Issue/JIRA pipelines always compose non-blank (the
    # anchor at minimum), so the replace also prevents a fork from
    # carrying the SOURCE pipeline's task text into this pipeline's
    # per-event prompts. A blank/None value (free-text resume with no
    # new prompt) preserves the pulled value so the source pipeline's
    # task statement still drives the resumed run.
    if task_description is not None and task_description.strip():
        contract.task_description = task_description
    save_contract(contract, repo_path)

    _pkg.logger.info(
        "Loaded contract from source branch",
        pipeline_id=pipeline_id,
        source_branch=source_branch,
        decision_count=len(contract.decisions),
        phase_count=len(contract.slices),
    )
    return True


def _read_phase_draft(
    repo_path: Path,
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    max_chars: int = 32000,
    branch: str | None = None,
) -> str | None:
    """Read draft file contents. Truncates at max_chars.

    Returns None when the draft cannot be found (no path configured or
    file missing on disk).

    Attempts in order:

    1. Primary (issue-specific) path on disk
    2. Generic (unprefixed) path on disk
    3. Primary path via ``git show origin/{branch}:``
    4. Generic path via ``git show origin/{branch}:``

    The ``git show`` fallback (steps 3–4) handles cases where
    ``_sync_worktree_with_remote`` failed silently and the draft exists
    on the remote branch but not in the local checkout.
    """
    draft_rel = _pkg._get_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)
    if not draft_rel:
        return None

    def _truncate(content: str) -> str:
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n... (truncated, {len(content)} chars total)"
        return content

    draft_path = repo_path / draft_rel
    generic_rel = _get_generic_draft_path(phase)

    # Try primary (issue-specific) path first.
    if draft_path.exists():
        return _truncate(draft_path.read_text(encoding="utf-8"))

    _pkg.logger.debug(
        "Draft file not found",
        path=str(draft_path),
        phase=phase,
        issue_number=issue_number,
        pipeline_id=pipeline_id,
    )

    # Fallback: try the generic (unprefixed) path on disk.
    if generic_rel:
        generic_path = repo_path / generic_rel
        if generic_path.exists():
            _pkg.logger.debug(
                "Using generic fallback draft path",
                primary_path=str(draft_path),
                fallback_path=str(generic_path),
                phase=phase,
            )
            return _truncate(generic_path.read_text(encoding="utf-8"))

    # Fallback: try reading from remote tracking ref via git show.
    # This handles cases where _sync_worktree_with_remote() failed
    # silently (fetch failure, detached HEAD, divergence, etc.) and
    # the draft exists on origin but not in the local checkout.
    if branch:
        content = _pkg._git_show_draft(repo_path, branch, draft_rel)
        if content is None and generic_rel:
            content = _pkg._git_show_draft(repo_path, branch, generic_rel)
        if content is not None:
            _pkg.logger.info(
                "Read draft from remote tracking ref (local copy missing)",
                phase=phase,
                branch=branch,
            )
            return _truncate(content)

    return None


def _read_human_phase_draft(
    repo_path: Path,
    phase: str,
    issue_number: int | None = None,
    pipeline_id: str | None = None,
    max_chars: int = 32000,
    branch: str | None = None,
) -> str | None:
    """Read the human-focused companion draft for a phase.

    Mirrors :func:`_read_phase_draft` (disk first, then the
    ``git show origin/{branch}`` fallback for a copy that only landed on
    the remote branch), but resolves the path via
    :func:`_get_human_draft_path` and has no generic-path variant — the
    companion is always pipeline-identified. Returns ``None`` when the
    companion is absent (so the gate falls back to the agent draft).
    """
    human_rel = _get_human_draft_path(phase, issue_number=issue_number, pipeline_id=pipeline_id)
    if not human_rel:
        return None

    def _truncate(content: str) -> str:
        if len(content) > max_chars:
            return content[:max_chars] + f"\n\n... (truncated, {len(content)} chars total)"
        return content

    human_path = repo_path / human_rel
    if human_path.exists():
        return _truncate(human_path.read_text(encoding="utf-8"))

    if branch:
        content = _pkg._git_show_draft(repo_path, branch, human_rel)
        if content is not None:
            _pkg.logger.info(
                "Read human companion draft from remote tracking ref (local copy missing)",
                phase=phase,
                branch=branch,
            )
            return _truncate(content)

    return None
