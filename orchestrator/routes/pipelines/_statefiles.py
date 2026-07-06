"""statefiles helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim; patched/barrel-resident globals reached via _pkg so
patch("routes.pipelines.<name>") keeps intercepting.
"""

from __future__ import annotations

import glob
import json
import subprocess
from pathlib import Path
from typing import Any

import routes.pipelines as _pkg  # noqa: E402,F401
from models import Pipeline


def _commit_statefiles_to_worktree(
    worktree_path: Path,
    message: str,
    pipeline_identifier: int | str | None = None,
    *,
    pipeline_id: str | None = None,
) -> bool:
    """Stage and commit ``.egg-state/`` files in *worktree_path*.

    When *pipeline_identifier* is provided, only files whose names start
    with the identifier (followed by ``.`` or ``-``) are staged.  This
    prevents concurrent pipelines from leaking each other's state files
    into unrelated PRs (see #1390).

    Most ``.egg-state/`` files are prefixed with the issue number (drafts,
    reviews, BRC history, agent-outputs), but contract files are keyed by
    ``pipeline_id`` (e.g. ``issue-1759-v3.json``) and don't share the
    issue-number prefix.  When *pipeline_id* is provided alongside
    *pipeline_identifier*, files matching either prefix are staged — this
    closes the gap where plan-phase contract updates were written to disk
    but never committed because the glob only saw the issue-number prefix
    (see #1829).

    Falls back to staging the entire ``.egg-state/`` directory when both
    *pipeline_identifier* and *pipeline_id* are ``None`` (backwards-compat).

    Any pre-existing staged changes in the worktree's index are discarded
    on entry — the helper runs ``git read-tree HEAD`` before staging (see
    :func:`_read_tree_head` for the cross-worktree-ref-advance defence
    from #2626).  Only files matching the pipeline scope and present on
    disk are committed; callers must not pre-stage state they expect this
    helper to preserve.

    The commit is idempotent (skips when nothing is staged).
    Raises ``subprocess.CalledProcessError`` on git failure.
    Call sites decide whether to abort or continue.

    Returns ``True`` when a commit was actually made, ``False`` when the
    helper short-circuited (no .egg-state dir, no prefix match, or
    nothing staged after add).  Lets call sites skip a follow-up push
    that would be a no-op fast-forward (#2548 review suggestion D).
    """
    state_dir = worktree_path / ".egg-state"
    _pkg.logger.info(
        "_commit_statefiles_to_worktree: entering",
        worktree_path=str(worktree_path),
        pipeline_identifier=str(pipeline_identifier),
        pipeline_id=str(pipeline_id),
        commit_message=message,
    )
    if not state_dir.exists():
        _pkg.logger.info(
            "_commit_statefiles_to_worktree: no .egg-state directory — exiting",
            worktree_path=str(worktree_path),
            pipeline_identifier=str(pipeline_identifier),
            pipeline_id=str(pipeline_id),
        )
        return False  # Nothing to commit yet

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_path}",
        "-C",
        str(worktree_path),
    ]

    if pipeline_identifier is not None or pipeline_id is not None:
        # Scope to files belonging to this pipeline only (#1390).
        # Use prefix-anchored patterns with delimiter boundaries to avoid
        # substring false positives (e.g. pipeline 4 matching pipeline 42).
        # Union both prefixes so issue-number-prefixed files (drafts,
        # reviews, BRC history) and pipeline-id-keyed files (contracts)
        # are all staged (#1829).
        prefixes: list[str] = []
        if pipeline_identifier is not None:
            prefixes.append(str(pipeline_identifier))
        if pipeline_id is not None and pipeline_id not in prefixes:
            prefixes.append(pipeline_id)

        matched_set: set[str] = set()
        for pid in prefixes:
            escaped = glob.escape(pid)
            pattern_dot = str(state_dir / "**" / f"{escaped}.*")
            pattern_dash = str(state_dir / "**" / f"{escaped}-*")
            for f in glob.glob(pattern_dot, recursive=True) + glob.glob(
                pattern_dash, recursive=True
            ):
                if Path(f).is_file():
                    matched_set.add(f)
        matched = sorted(matched_set)
        _pkg.logger.info(
            "_commit_statefiles_to_worktree: glob match results",
            pipeline_identifier=str(pipeline_identifier),
            pipeline_id=str(pipeline_id),
            prefixes=prefixes,
            match_count=len(matched),
            matched_paths=[str(Path(f).relative_to(worktree_path)) for f in matched[:20]],
            truncated=len(matched) > 20,
        )
        if not matched:
            return False  # No state files for this pipeline yet

        rel_paths = [str(Path(f).relative_to(worktree_path)) for f in matched]
        _pkg._read_tree_head(git_base)
        # Restore scope is intentionally broader than the staging glob:
        # the helper operates over all of ``.egg-state/`` to maintain
        # HEAD↔disk parity (so other readers — e.g. peer-artifact loads —
        # see what HEAD says).  Each pipeline has its own worktree, so
        # broader scope cannot resurrect a sibling-pipeline file.
        _pkg._restore_missing_state_files_from_head(git_base, worktree_path, pipeline_id)
        subprocess.run(
            [*git_base, "add", "--force", "--"] + rel_paths,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    else:
        _pkg._read_tree_head(git_base)
        # Restore scope matches the staging scope here — both span all of
        # ``.egg-state/`` — so the broader restore is trivially safe.
        _pkg._restore_missing_state_files_from_head(git_base, worktree_path, pipeline_id)
        subprocess.run(
            [*git_base, "add", "--force", ".egg-state/"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    # Only commit if there are staged changes (idempotent on re-runs).
    # No pathspec: match the diff scope to the commit scope below so the
    # early-out fires iff the commit would have nothing to write. A
    # scoped diff (``-- .egg-state/``) paired with the unscoped commit
    # below would short-circuit when only non-``.egg-state/`` content
    # is staged, dropping that content on the floor instead of
    # committing it. Nothing in this code path stages outside
    # ``.egg-state/`` today, so this is belt-and-suspenders, but the
    # two scopes must stay symmetric to keep the invariant local.
    result = subprocess.run(
        [*git_base, "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode == 0:
        _pkg.logger.info(
            "_commit_statefiles_to_worktree: nothing staged — skipping commit",
            pipeline_identifier=str(pipeline_identifier),
            commit_message=message,
        )
        return False  # Nothing to commit

    _pkg.logger.info(
        "_commit_statefiles_to_worktree: staged changes detected — committing",
        pipeline_identifier=str(pipeline_identifier),
        commit_message=message,
    )
    # Commit WITHOUT a trailing ``-- .egg-state/`` pathspec.  ``git commit``
    # with a pathspec defaults to ``--only`` semantics, which auto-stages
    # working-tree changes (including unstaged *deletions*) for the
    # matching paths — i.e. ``git commit -- .egg-state/`` silently picks
    # up files that disappeared from disk even though the explicit
    # ``git add`` above only staged the on-disk hits from the glob.  This
    # surfaces as two distinct failure shapes that share the same
    # mechanism (HEAD references a draft that is not on disk locally):
    # #2625, where agents push drafts to ``origin/<branch>`` from their
    # own worktrees so the orchestrator's local checkout sits at a HEAD
    # containing files it never materialised; and #2626, where the
    # agent-side ``git update-ref`` recovery (plumbing, no per-worktree
    # branch lock) advances the shared pipeline-branch ref under the
    # orchestrator's worktree, leaving every agent-pushed file looking
    # like a staged deletion. In both cases the pathspec form turned a
    # benign working-tree gap into a delete-commit against agent-pushed
    # work. Without the pathspec, only the explicit ``git add`` staging
    # above is committed.
    subprocess.run(
        [*git_base, "commit", "--no-verify", "-m", message],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    _pkg.logger.info(
        "_commit_statefiles_to_worktree: commit succeeded",
        pipeline_identifier=str(pipeline_identifier),
        commit_message=message,
    )
    return True


def persist_contract_statefiles(
    pipeline_id: str,
    worktree_path: Path,
    message: str,
    *,
    pipeline: Pipeline | None = None,
) -> bool:
    """Durably persist a contract decision write: commit + push to the work branch.

    Contract HITL decisions (``cq-N`` registrations and resolutions) are
    written to the shared pipeline worktree's contract file with no git
    commit; the file was only serialized to the work branch at slice/phase
    checkpoints. Both phase-(re)start syncs — the gateway's worktree-reuse
    reset and ``_sync_worktree_with_remote`` step 4 — run
    ``git reset --hard origin/<work>``, so any decision write that had not
    been committed AND pushed by then was silently reverted, letting the
    bootstrap reconciler re-mint the same ``cq-N`` ids and clobber
    just-resolved operator decisions (#3427). Committing and pushing at
    write time makes the reset target already contain the decision.

    Best-effort by design: failures are logged and swallowed — the write is
    still live on the worktree file and the next checkpoint commit retries.
    Returns ``True`` only when the state was committed and pushed (or there
    was nothing new to commit).
    """
    try:
        if pipeline is None:
            _, pipeline = _pkg._resolve_pipeline(pipeline_id, _pkg.get_repo_path())
        identifier = _pkg._pipeline_identifier(getattr(pipeline, "issue_number", None), pipeline_id)
        committed = _pkg._commit_statefiles_to_worktree(
            worktree_path,
            message,
            identifier,
            pipeline_id=pipeline_id,
        )
        if not committed:
            return True  # Nothing new on disk — already durable.
        branch = getattr(pipeline, "branch", None)
        if not branch:
            _pkg.logger.warning(
                "Contract decision write committed but pipeline has no work "
                "branch to push to; the commit is local-only and a worktree "
                "reset may still discard it (#3427)",
                pipeline_id=pipeline_id,
            )
            return False
        gateway_mode, _ = _pkg._compute_gateway_mode(pipeline)
        _pkg._get_spawner().gateway.push_worktree_branch(
            pipeline_id=pipeline_id,
            repo_path=str(worktree_path),
            branch=branch,
            mode=gateway_mode,
            base_branch=getattr(pipeline, "base_branch", None),
        )
        _pkg.logger.info(
            "Contract decision write persisted to work branch (#3427)",
            pipeline_id=pipeline_id,
            branch=branch,
            commit_message=message,
        )
        return True
    except Exception as persist_err:  # noqa: BLE001 — best-effort durability
        _pkg.logger.warning(
            "Failed to durably persist contract decision write; the decision "
            "is live on the worktree file but will not survive a worktree "
            "reset until the next checkpoint commit (#3427)",
            pipeline_id=pipeline_id,
            error=str(persist_err),
        )
        return False


def _ensure_statefiles_on_branch(
    worktree_repo_path: Path,
    pipeline: Pipeline,
) -> bool:
    """Verify the contract file exists in the worktree and re-create if missing.

    This is a safety net for short-flow pipelines where the initial contract
    push may have failed or where subsequent pushes diverged.

    Returns True if the contract exists (or was successfully restored),
    False if restoration failed.
    """
    from egg_contracts.loader import contract_exists, create_contract, get_contract_path

    # Contract lookup uses pipeline.id directly (canonical key).
    if contract_exists(pipeline.id, worktree_repo_path):
        return True

    canonical_path = get_contract_path(pipeline.id, worktree_repo_path)

    _pkg.logger.warning(
        "Contract file missing from worktree — attempting restoration",
        pipeline_id=pipeline.id,
        expected_path=str(canonical_path),
    )

    try:
        # Mirror the primary creation site: the composed task statement
        # (identity anchor + submit description, #3163) lands on the
        # restored contract too, for every entry path.
        from egg_contracts.loader import compose_task_description

        issue_url = (
            f"https://github.com/{pipeline.repo}/issues/{pipeline.issue_number}"
            if pipeline.issue_number is not None
            else None
        )
        task_description = compose_task_description(
            description=pipeline.prompt,
            issue_number=pipeline.issue_number,
            issue_url=issue_url,
            jira_ticket=pipeline.jira_ticket,
        )
        if pipeline.issue_number is not None:
            create_contract(
                issue_number=pipeline.issue_number,
                title=f"Issue #{pipeline.issue_number}",
                url=issue_url or "",
                pipeline_id=pipeline.id,
                repo_root=worktree_repo_path,
                task_description=task_description,
            )
        else:
            create_contract(
                pipeline_id=pipeline.id,
                title=(pipeline.prompt or "")[:100],
                task_description=task_description,
                repo_root=worktree_repo_path,
            )

        # Restore plan/analysis drafts from remote if missing locally.
        # These were pushed during init but may be lost from the worktree
        # after agent activity during the implement phase (#1454).
        if pipeline.branch:
            git_base = [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                f"safe.directory={worktree_repo_path}",
                "-C",
                str(worktree_repo_path),
            ]
            # Ensure remote-tracking ref is fresh before reading from it.
            try:
                subprocess.run(
                    [*git_base, "fetch", "origin", pipeline.branch],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except Exception:
                pass  # Best-effort; git show may still work with cached ref
            for draft_phase in ("plan", "refine"):
                draft_rel = _pkg._get_draft_path(
                    draft_phase,
                    issue_number=pipeline.issue_number,
                    pipeline_id=pipeline.id,
                )
                if not draft_rel:
                    continue
                draft_path = worktree_repo_path / draft_rel
                if draft_path.exists():
                    continue
                try:
                    result = subprocess.run(
                        [*git_base, "show", f"origin/{pipeline.branch}:{draft_rel}"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    if result.returncode == 0 and result.stdout:
                        draft_path.parent.mkdir(parents=True, exist_ok=True)
                        draft_path.write_text(result.stdout, encoding="utf-8")
                        _pkg.logger.info(
                            "Restored draft from remote branch",
                            pipeline_id=pipeline.id,
                            draft_path=draft_rel,
                        )
                except Exception as e:
                    _pkg.logger.warning(
                        "Could not restore draft from remote",
                        pipeline_id=pipeline.id,
                        draft_path=draft_rel,
                        error=str(e),
                    )

        # Final fallback: write plan/analysis from pipeline model if still
        # missing after remote restoration attempt.  This handles the case
        # where the draft was never pushed to the remote (#1460).
        for draft_phase, field_value in [("plan", pipeline.plan), ("refine", pipeline.analysis)]:
            if not field_value:
                continue
            draft_rel = _pkg._get_draft_path(
                draft_phase,
                issue_number=pipeline.issue_number,
                pipeline_id=pipeline.id,
            )
            if not draft_rel:
                continue
            draft_path = worktree_repo_path / draft_rel
            if draft_path.exists():
                continue
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(field_value, encoding="utf-8")
            _pkg.logger.info(
                "Restored draft from pipeline model (remote unavailable)",
                pipeline_id=pipeline.id,
                draft_path=draft_rel,
            )

        # Re-populate tasks and PR metadata from plan draft if available.
        # Without this, recreated contracts lose the planner-generated PR
        # title/description and fall back to the generic pipeline ID title.
        # See: https://github.com/jwbron/egg/issues/1432
        _restore_populate_result = _pkg._populate_contract_from_plan(
            worktree_repo_path,
            pipeline.id,
            pipeline.mode.value if pipeline.mode else "issue",
            pipeline.issue_number,
        )
        # #2627 follow-up: this is a best-effort restoration path on a
        # recreated contract — failure here is recoverable on later
        # pipeline steps, so we just log the structured outcome.
        if _restore_populate_result.outcome != _pkg.PopulateOutcome.POPULATED:
            _pkg.logger.info(
                "Restored-contract populate produced non-POPULATED outcome",
                pipeline_id=pipeline.id,
                outcome=_restore_populate_result.outcome.value,
            )

        # File-staging identifier still uses _pipeline_identifier convention.
        identifier = _pkg._pipeline_identifier(pipeline.issue_number, pipeline.id)
        _pkg._commit_statefiles_to_worktree(
            worktree_repo_path,
            f"Restore missing contract for {identifier}",
            pipeline_identifier=identifier,
            pipeline_id=pipeline.id,
        )
        _pkg.logger.info(
            "Contract file restored successfully",
            pipeline_id=pipeline.id,
        )
        return True
    except Exception as restore_err:
        _pkg.logger.error(
            "Failed to restore contract file",
            pipeline_id=pipeline.id,
            error=str(restore_err),
        )
        return False


def _detect_default_branch(worktree_repo_path: Path) -> str:
    """Detect the remote's default branch from a worktree.

    Tries in order:
    1. origin/HEAD symbolic ref (most reliable)
    2. origin/main
    3. origin/master
    4. Fallback to "main"

    Returns:
        The branch name (e.g., "main" or "master"), without the "origin/" prefix.
    """
    # Try origin/HEAD symbolic ref
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            ref = result.stdout.strip()  # e.g. "origin/main"
            return ref.removeprefix("origin/")
    except Exception:
        pass

    # Try origin/main
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/main"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return "main"
    except Exception:
        pass

    # Try origin/master
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "origin/master"],
            capture_output=True,
            text=True,
            cwd=str(worktree_repo_path),
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return "master"
    except Exception:
        pass

    _pkg.logger.warning(
        "Could not detect default branch, falling back to 'main'",
        worktree_path=str(worktree_repo_path),
    )
    return "main"


def _resolve_origin_ref(base_branch: str | None) -> str:
    """Return ``origin/<branch>``, falling back to ``origin/main``.

    Centralises the ``f"origin/{base_branch}" if base_branch else "origin/main"``
    pattern so every orient-prompt / diff-command call site honours the
    resolved base branch consistently.
    """
    ref = (base_branch or "main").strip() or "main"
    # Tolerate callers that already passed ``origin/<x>`` by mistake.
    if ref.startswith("origin/"):
        return ref
    return f"origin/{ref}"


def _fetch_pr_state(pr_number: int, repo: str | None = None) -> dict[str, Any]:
    """Fetch PR state, base/head refs, and fork-hint via ``gh pr view``.

    Returns a dict with keys ``state`` (str, e.g. "OPEN"/"MERGED"/"CLOSED"),
    ``base_ref`` (str or None), ``head_ref`` (str or None), ``head_sha``
    (str or None), ``is_fork`` (bool), ``changed_files`` (int), and
    ``head_repository_name_with_owner`` (str or None).  Returns an empty
    dict when ``gh`` is unavailable or the PR cannot be looked up.
    """
    if pr_number is None:
        return {}
    fields = (
        "state,baseRefName,headRefName,headRefOid,isCrossRepository,"
        "changedFiles,headRepositoryOwner,headRepository"
    )
    cmd = ["gh", "pr", "view", str(pr_number), "--json", fields]
    if repo:
        cmd.extend(["--repo", repo])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _pkg.logger.warning(
            "_fetch_pr_state: gh pr view raised",
            pr_number=pr_number,
            repo=repo,
            error=str(exc),
        )
        return {}
    if result.returncode != 0:
        _pkg.logger.warning(
            "_fetch_pr_state: gh pr view failed",
            pr_number=pr_number,
            repo=repo,
            returncode=result.returncode,
            stderr=result.stderr.strip()[:200],
        )
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError, ValueError:
        return {}

    head_repo = data.get("headRepository") or {}
    head_owner = data.get("headRepositoryOwner") or {}
    head_repo_name = head_repo.get("name") if isinstance(head_repo, dict) else None
    head_owner_login = head_owner.get("login") if isinstance(head_owner, dict) else None
    head_repo_full = (
        f"{head_owner_login}/{head_repo_name}" if head_owner_login and head_repo_name else None
    )
    return {
        "state": data.get("state"),
        "base_ref": data.get("baseRefName"),
        "head_ref": data.get("headRefName"),
        "head_sha": data.get("headRefOid"),
        "is_fork": bool(data.get("isCrossRepository")),
        "changed_files": data.get("changedFiles") or 0,
        "head_repository_name_with_owner": head_repo_full,
    }
