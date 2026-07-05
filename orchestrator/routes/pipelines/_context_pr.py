"""context pr helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim; patched/barrel-resident globals reached via _pkg so
patch("routes.pipelines.<name>") keeps intercepting.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import routes.pipelines as _pkg  # noqa: E402,F401
from egg_contracts.markdown import unwrap_soft_breaks
from models import PipelinePhase

from ._drafts import _get_human_draft_path


def _build_pre_merge_obligations_section(
    pipeline_id: str,
    contract_deferred_actions: list[Any] | None = None,
) -> str:
    """Render the "Pre-merge Obligations" section from active conditional ACKs.

    Two sources, in order of preference:

    1. ``contract_deferred_actions`` — ``DeferredAction`` objects (or legacy
       strings) previously persisted to ``contract.pr.deferred_actions`` when
       a human approved the conditional-ACK HITL gate (#2004). This is the
       durable path: the tracker may have been torn down by the time PR
       creation runs, and the contract survives.
    2. The live consensus tracker (#1998). Used when the contract has
       no deferred_actions — either because the gate landed before
       tracker teardown, or the gate was never required.

    The markdown composition (open vs. resolved sections, banner copy)
    is delegated to :mod:`orchestrator.pr_obligations`. Pre-#2777 cq-6
    the slice-DAG terminal slice rendered the same section from this
    shared shape; under cq-4 the obligations live solely on the
    up-front context PR (``egg/<id>/work → main``) opened by
    :func:`_open_context_pr_at_implement_start`, so only this
    ``_auto_create_pr`` callsite renders them now. The shared shape
    stays so a future caller (re-introducing per-slice obligation
    rendering, etc.) has parity.

    Returns an empty string if neither source yields obligations, so
    callers can unconditionally append the result to the PR body.
    """
    try:
        from pr_obligations import render_obligations_section_from_normalized
    except ImportError:
        from ..pr_obligations import (  # type: ignore[import-not-found,no-redef]
            render_obligations_section_from_normalized,
        )
    obligations = _collect_pre_merge_obligations(pipeline_id, contract_deferred_actions)
    return render_obligations_section_from_normalized(obligations)


def _collect_pre_merge_obligations(
    pipeline_id: str,
    contract_deferred_actions: list[Any] | None,
) -> list[dict[str, str]]:
    """Normalize obligations from contract or live tracker into a uniform shape.

    Returns a list of ``{reviewer, condition, resolved_in_diff}`` dicts. The
    contract source takes precedence over the live tracker when present.

    .. note::

       Under #2777 cq-4 obligations live on the up-front context PR
       (``egg/<id>/work → main``) opened by
       :func:`_open_context_pr_at_implement_start`, not on individual
       slice PRs — so the slice-loop no longer calls this helper. The
       pipeline-level tracker fallback survives because the
       ``_auto_create_pr`` path that still calls this helper uses the
       pipeline-level tracker; future re-introducers of per-slice
       obligation rendering would need to thread a slice-keyed tracker
       (see ``peer_consensus._tracker_key`` ⇒
       ``{pipeline_id}/{slice_id}``) through here.
    """
    try:
        from pr_obligations import normalize_deferred_actions
    except ImportError:
        from ..pr_obligations import (  # type: ignore[import-not-found,no-redef]
            normalize_deferred_actions,
        )
    normalized = normalize_deferred_actions(contract_deferred_actions)
    if normalized:
        return normalized

    # Tier 2 — live tracker (pre-#2004 path; kept so conditions still
    # render if the HITL gate hasn't resolved yet, e.g. under force=true).
    try:
        from peer_consensus import get_peer_consensus_tracker
    except ImportError:
        from ..peer_consensus import get_peer_consensus_tracker  # type: ignore[import-not-found]
    tracker = get_peer_consensus_tracker(pipeline_id)
    if tracker is None:
        return []
    try:
        conditions = tracker.get_pre_merge_conditions()
    except Exception as e:  # defensive — never block PR creation on this
        _pkg.logger.warning(
            "Failed to read pre-merge conditions from tracker",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return []

    tracker_normalized: list[dict[str, str]] = []
    for c in conditions:
        condition = str(c.get("condition", "")).strip()
        if not condition:
            continue
        tracker_normalized.append(
            {
                "reviewer": str(c.get("reviewer", "") or "").strip(),
                "condition": condition,
                "resolved_in_diff": str(c.get("resolved_in_diff", "") or "").strip(),
            }
        )
    return tracker_normalized


def _build_brc_history_link_line(
    worktree_repo_path: Path,
    identifier: int | str | None,
    link_base: str | None = None,
) -> str:
    """Build a one-line pointer to the committed BRC history transcripts.

    Scans ``.egg-state/brc-history/`` for ``{identifier}-<phase>.md`` files
    written by :func:`_write_brc_history` and returns a sentence linking
    each phase's transcript, ordered by canonical execution order
    (``refine`` → ``plan`` → ``implement`` → ``pr``; unknown names sorted
    alphabetically after).

    ``link_base`` (#3115): when set (e.g.
    ``https://github.com/<repo>/blob/<branch>``), links are rendered as
    branch-qualified absolute URLs instead of the default ``./``-relative
    form. GitHub resolves relative links in PR bodies against the repo's
    default branch, where ``.egg-state/`` does not exist — so any caller
    embedding this line in a PR body must pass ``link_base``.

    Returns an empty string when ``identifier`` is ``None`` or no
    transcripts exist on disk.
    """
    if identifier is None:
        return ""
    history_dir = worktree_repo_path / ".egg-state" / "brc-history"
    if not history_dir.is_dir():
        return ""
    prefix = f"{identifier}-"
    phases: list[str] = []
    for path in history_dir.glob(f"{prefix}*.md"):
        stem = path.stem
        if stem.startswith(prefix):
            phases.append(stem[len(prefix) :])
    if not phases:
        return ""

    canonical = [p.value for p in PipelinePhase]
    rank = {name: i for i, name in enumerate(canonical)}

    # Per-slice implement files (#2548) carry the stem
    # ``implement-slice-{N}``; cluster them at the canonical ``implement``
    # rank so the rendered link order is
    # ``refine → plan → implement[-slice-N] → implement-unattributed →
    # pr`` instead of pushing the per-slice files past pr to the end of
    # the list. Within the implement cluster, sort by the integer slice
    # index so a 12-slice pipeline renders ``slice-1, slice-2, …,
    # slice-12`` rather than the lexicographic ``slice-1, slice-10,
    # slice-11, slice-12, slice-2``. The ``implement-unattributed``
    # sibling (cross-cutting non-CONSENSUS BRC types without slice scope,
    # see ``_write_brc_history``) sorts after every per-slice file so a
    # reviewer reads each slice transcript first, then the cross-cutting
    # context.
    def _sort_key(name: str) -> tuple[int, int, str]:
        if name == "implement":
            return (rank["implement"], -1, "")
        if name == "implement-unattributed":
            return (rank["implement"], 1 << 30, name)
        if name.startswith("implement-slice-"):
            try:
                idx = int(name.rsplit("-", 1)[1])
            except ValueError:
                idx = 1 << 30  # malformed → sort last within cluster
            return (rank["implement"], idx, name)
        return (rank.get(name, len(canonical)), 0, name)

    phases.sort(key=_sort_key)

    prefix_url = f"{link_base.rstrip('/')}/" if link_base else "./"
    links = ", ".join(
        f"[`{phase}`]({prefix_url}.egg-state/brc-history/{identifier}-{phase}.md)"
        for phase in phases
    )
    return f"_Per-phase BRC transcripts: {links}._"


def _compose_context_pr_body(
    *,
    contract,
    pipeline,
    worktree_repo_path: Path,
    identifier: int | str,
    context_repo: str | None = None,
    sibling_context_prs: list[dict[str, Any]] | None = None,
) -> str:
    """Compose the context-PR body from contract + pipeline state (#3115).

    Before #3115 the context PR's body was ``contract.pr.description``
    verbatim, which dropped ``test_plan`` / ``manual_steps`` on the
    floor (the composer that rendered them died with the PR phase in
    #2777 even though the plan preflight still requires both fields)
    and linked to none of the pipeline artifacts the orchestrator
    deterministically knows about. This helper restores the full shape:

    1. The planner's ``description`` (narrative, verbatim).
    2. ``## Test Plan`` / ``## Manual Steps`` from the contract fields
       (Title Case matches the global PR template and the slice PR's
       inline-narrative branch in ``gateway_client.py``).
    3. A generated ``## Pipeline context`` footer: pipeline id,
       originating issue, the slice table, and links to the refine
       analysis draft, the plan draft, and the per-phase BRC
       transcripts committed on the work branch.

    Artifact links are branch-qualified absolute URLs
    (``https://github.com/<repo>/blob/<work-branch>/...``) — GitHub
    resolves relative links in PR bodies against the default branch,
    where ``.egg-state/`` does not exist. Draft links are only emitted
    for files that exist in the worktree, so a pipeline that skipped
    refine does not link a 404.

    Pure string composition over already-loaded state — no git or
    gateway calls — so the opener's failure surface is unchanged.
    """
    pr = contract.pr
    sections: list[str] = []

    # Soft-break unwrapping (#3122): the ``pr:`` block fields arrive as
    # YAML block scalars hard-wrapped at ~75 chars, and GitHub renders
    # every newline in a PR body as a line break — join the wraps back
    # into paragraphs, leaving real markdown structure alone.
    description = unwrap_soft_breaks(pr.description if pr else None).strip()
    if description:
        sections.append(description)

    test_plan = unwrap_soft_breaks(pr.test_plan if pr else None).strip()
    if test_plan:
        sections.append(f"## Test Plan\n\n{test_plan}")

    manual_steps = unwrap_soft_breaks(pr.manual_steps if pr else None).strip()
    if manual_steps:
        sections.append(f"## Manual Steps\n\n{manual_steps}")

    # Build the footer body first; only emit the ``## Pipeline context``
    # header when *more than* the bare pipeline-id line gets added (a
    # single ``- Pipeline: <id>`` line under its own ``##`` header is
    # noise — every reviewer can read that off the URL).
    body_lines: list[str] = [f"- Pipeline: `{pipeline.id}`"]
    has_meaningful_content = False
    if pipeline.issue_number:
        # Bare ``#N`` autolinks within the same repo, which is where
        # the pipeline's originating issue lives.
        body_lines.append(f"- Issue: #{pipeline.issue_number}")
        has_meaningful_content = True

    # #3393 slice-4 / task-4-2: the repo this context PR lives in. A
    # slice PR in this same repo cross-links as a bare ``#N`` autolink;
    # a slice PR in a DIFFERENT repo of the pipeline must be qualified
    # as ``owner/repo#N`` (a bare ``#N`` would resolve against the wrong
    # repo). Defaults to the pipeline primary — the repo the up-front
    # opener composes the primary context PR for. For an N=1 pipeline
    # every slice resolves to the primary, so every link stays bare and
    # the body is byte-identical to the single-repo shape.
    this_context_repo = context_repo or getattr(pipeline, "primary_repo", None) or pipeline.repo
    try:
        from models import resolve_slice_repo  # type: ignore[no-redef]
    except ImportError:
        from ..models import resolve_slice_repo  # type: ignore[no-redef]

    slices = list(contract.slices or [])
    if slices:
        body_lines.append(f"- Slices ({len(slices)}):")
        for s in slices:
            name = " ".join((s.name or s.id).split())
            # Strip both ``slice-`` and the legacy ``phase-`` prefix —
            # ``Slice.id`` still permits the latter (models.py) and
            # ``_migrate_phases_to_slices`` only rewrites it on JSON
            # load, so a directly-constructed Slice can still carry it.
            number = s.id.removeprefix("slice-").removeprefix("phase-")
            line = f"  {number}. {name} (`{s.id}`)"
            # Cross-link the stack (#3122): once the slice's PR is open
            # its number is persisted on the contract and the run loop
            # re-composes this body, so the entry gains a link.
            if getattr(s, "pr_number", None):
                s_repo = resolve_slice_repo(s, pipeline)
                if s_repo and this_context_repo and s_repo != this_context_repo:
                    # Cross-repo sibling — repo-qualify so GitHub resolves
                    # the autolink to the right repo (#3393 slice-4).
                    line += f" — {s_repo}#{s.pr_number}"
                else:
                    # Same-repo (or repo unknown): bare ``#N`` autolinks
                    # within the repo this context PR lives in.
                    line += f" — #{s.pr_number}"
            body_lines.append(line)
        has_meaningful_content = True

    link_base: str | None = None
    if pipeline.repo and pipeline.branch:
        link_base = f"https://github.com/{pipeline.repo}/blob/{pipeline.branch}"

    if link_base:
        doc_links: list[str] = []
        for phase, label in (("refine", "Refine analysis"), ("plan", "Implementation plan")):
            rel_path = _pkg._get_draft_path(
                phase, issue_number=pipeline.issue_number, pipeline_id=pipeline.id
            )
            if rel_path and (worktree_repo_path / rel_path).is_file():
                doc_links.append(f"[{label}]({link_base}/{rel_path})")
            # Human-focused companion (the simplifier's ``*-human.md``), when present.
            human_rel = _get_human_draft_path(
                phase, issue_number=pipeline.issue_number, pipeline_id=pipeline.id
            )
            if human_rel and (worktree_repo_path / human_rel).is_file():
                doc_links.append(f"[{label} (human summary)]({link_base}/{human_rel})")
        if doc_links:
            body_lines.append(f"- Docs: {', '.join(doc_links)}")
            has_meaningful_content = True
        brc_line = _build_brc_history_link_line(worktree_repo_path, identifier, link_base=link_base)
        if brc_line:
            body_lines.append("")
            body_lines.append(brc_line)
            has_meaningful_content = True

    if has_meaningful_content:
        sections.append("\n".join(["## Pipeline context", "", *body_lines]))

    # #3393 slice-4 / task-4-2: cross-reference the pipeline's context
    # PRs in OTHER repos. Rendered only for a multi-repo pipeline (the
    # opener passes ``sibling_context_prs`` when it coordinates >1
    # repo); an N=1 pipeline passes ``None`` and this section is
    # omitted, keeping the body byte-identical to the single-repo shape.
    coord_lines: list[str] = []
    for ref in sibling_context_prs or []:
        ref_repo = (ref.get("repo") or "").strip()
        ref_number = ref.get("number")
        if not ref_repo or not isinstance(ref_number, int) or isinstance(ref_number, bool):
            continue
        if ref_number < 1:
            continue
        # ``owner/repo#N`` autolinks cross-repo (a bare ``#N`` would
        # resolve against the repo this body lives in).
        coord_lines.append(f"- {ref_repo}#{ref_number}")
    if coord_lines:
        sections.append(
            "\n".join(
                [
                    "## Coordinated repos",
                    "",
                    "This pipeline coordinates PRs across multiple repos (#3393):",
                    "",
                    *coord_lines,
                ]
            )
        )
    return "\n\n".join(sections)


def _persist_context_pr_number(
    pipeline_id: str,
    pr_number: int,
    *,
    worktree_repo_path: Path,
    identifier: int | str,
    pr_url: str | None = None,
) -> None:
    """Persist context-PR linkage on both the contract and the pipeline (#2777).

    Single-purpose helper extracted so the new
    :func:`_open_context_pr_at_implement_start` opener is not a
    non-transactional state mutator. Wraps the contract write under
    the existing per-pipeline state lock so concurrent advance_phase /
    backstop callers serialise on the same lock instance the rest of
    the orchestrator uses, then calls ``save_contract`` to atomically
    rewrite ``.egg-state/contracts/...`` on disk.

    The helper is the SOLE writer of ``context_pr_number`` after
    slice-2 (#2777, TASK-2-1) deleted the legacy
    ``_persist_context_pr_linkage_on_contract``. It is called exactly
    once per ``_open_context_pr_at_implement_start`` invocation,
    immediately after either the ``gh pr list`` idempotency hit or the
    successful ``gh pr create``. The same persistence write fires on
    the idempotent path so a resume-from-orphaned-pipeline where the
    contract lost ``context_pr_number`` mid-run still recovers (the
    unit test in TASK-3-8 asserts this).

    In slice-2 (#2777 TASK-2-2 cross-reviewer NACK fix) the helper was
    extended to ALSO write ``pipeline.pr_url`` and ``pipeline.pr_number``
    on the pipeline record. Three downstream consumers depend on these
    pipeline-level fields:

    * :func:`_get_pr_info` at the pipeline-status endpoint
      (``/api/v1/pipelines/<id>/status``) reports them.
    * :meth:`PipelineToolHandler._make_pipeline_summary` (the MCP
      ``get_pipeline_status`` tool) reports them.
    * ``orchestrator.jira_reassess.pipelines_for_ticket_pr_url`` powers
      the #1557 reverse-index in-flight detection that prevents the
      Jira reassess sweep from re-mutating issues whose parent egg run
      still has an open PR.

    Before this rewire the dedicated writer for the pipeline fields was
    the deleted ``_finalize_pr_phase_failed`` (TASK-2-2 of #2777
    deleted it lock-step with the PR phase). Without the explicit
    rewrite each of the three consumers above would silently report
    ``None``.

    ``pr_url`` is synthesised from ``pipeline.repo`` + ``pr_number``
    when not supplied (the idempotent ``gh pr list`` hit only carries
    the number; the create_pr path knows the URL directly from gh's
    stdout). The synthesis mirrors GitHub's canonical PR URL shape and
    keeps ``_get_pr_info``'s regex parse working unchanged.

    Persistence surface (egg-reviewer non-blocking #3):

        ``save_contract`` is a file-level atomic write — it rewrites
        the contract on disk but does NOT commit-and-push it to the
        worktree branch. The legacy
        ``_persist_context_pr_linkage_on_contract`` (slice-2 deletes
        it) wrapped the save in
        ``_commit_statefiles_to_worktree`` + ``push_worktree_branch``;
        the new opener intentionally does NOT, because the opener
        runs at the canonical advance_phase REST site BEFORE
        ``_spawn_pipeline_run_thread`` spawns the runner. That makes
        the on-disk write durable for the runner's first read, but
        the runner's ``_sync_worktree_with_remote`` has hard-reset
        paths that can later wipe an uncommitted contract change.
        Convergence is by the four runner-side backstops (slice-loop
        entry, implement-entry backstop, ``_run_pipeline`` auto-
        advance, HITL resume), which call the opener again — its
        ``gh pr list`` idempotency hit re-persists ``context_pr_number``
        on disk after a reset. Across the full lifecycle the persisted
        value converges; within a single advance_phase call the helper
        is best-effort-on-disk-pending-runner-commit, not transactional.

    Raises:
        ContextPrCreationError: when the contract cannot be loaded or
            saved. Unlike the soft-fail legacy helper this propagates
            so the caller surfaces a typed failure rather than leaving
            the contract out-of-sync with GitHub.
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError as imp_err:
        raise _pkg.ContextPrCreationError(
            "egg_contracts.loader unavailable while persisting context_pr_number",
            reason="loader_unavailable",
            cause=imp_err,
        ) from imp_err

    try:
        with _pkg.get_pipeline_state_lock(pipeline_id):
            contract_local = load_contract(identifier, worktree_repo_path)
            if contract_local.pr is None:
                # The contract MUST have a PR record by the time we
                # reach the plan→implement boundary — populate writes
                # it from the plan's ``pr:`` block. Missing PRMetadata
                # here is a structural failure, not a persistence
                # nuance; surface it loudly.
                raise _pkg.ContextPrCreationError(
                    "contract has no PRMetadata; cannot persist "
                    "context_pr_number (populate-from-plan must run first)",
                    reason="missing_pr_metadata",
                )
            contract_local.pr.context_pr_number = pr_number
            save_contract(contract_local, worktree_repo_path)

            # Pipeline-level mirror (#2777 cross-reviewer NACK fix).
            # Load → mutate → save under the same lock so the contract
            # write and pipeline write are atomic for downstream
            # observers (status endpoint, MCP tool, jira_reassess).
            # Pull the state store via the same lazy-import pattern the
            # rest of pipelines.py uses; the soft-fail import shape is
            # intentional so a stripped-down test harness that mocks
            # only the contract loader does not crash here.
            # ``get_state_store`` requires the repo path explicitly
            # (state_store.py:1356); pass ``worktree_repo_path`` so the
            # store resolves under the same root we just wrote the
            # contract to.
            try:
                from state_store import get_state_store  # type: ignore[no-redef]
            except ImportError:
                from ..state_store import get_state_store  # type: ignore[no-redef]
            store = get_state_store(worktree_repo_path)
            try:
                reloaded = store.load_pipeline(pipeline_id)
            except Exception as pipe_load_err:  # noqa: BLE001
                # Don't fail the whole opener because the pipeline
                # mirror couldn't be loaded — the contract write
                # already succeeded above. Log + continue so the
                # context PR opens; the mirror will be re-applied
                # on the next idempotent opener tick.
                _pkg.logger.warning(
                    "Context PR opener: could not mirror pipeline.pr_url / "
                    "pipeline.pr_number (continuing — contract write succeeded)",
                    pipeline_id=pipeline_id,
                    pr_number=pr_number,
                    error=str(pipe_load_err),
                )
                return
            mirror_url = pr_url
            if mirror_url is None:
                # Idempotent path (``gh pr list`` hit) only carries the
                # number; synthesise the canonical PR URL from
                # pipeline.repo + pr_number so all three consumers
                # still see a populated ``pr_url`` string. Skip the
                # synthesis when ``repo`` is unset (local-mode
                # pipelines have no remote PR).
                if reloaded.repo:
                    mirror_url = f"https://github.com/{reloaded.repo}/pull/{pr_number}"
            reloaded.pr_number = pr_number
            if mirror_url:
                reloaded.pr_url = mirror_url
            store.save_pipeline(reloaded)
    except _pkg.ContextPrCreationError:
        raise
    except Exception as save_err:  # noqa: BLE001
        raise _pkg.ContextPrCreationError(
            f"failed to persist context_pr_number={pr_number}: {save_err}",
            reason="save_failed",
            cause=save_err,
        ) from save_err


def _refresh_context_pr_body(
    pipeline_id: str,
    *,
    pipeline: Any,
    spawner: Any,
    worktree_repo_path: Path,
    identifier: int | str,
    gateway_mode: str = "public",
) -> bool:
    """Re-compose and push the context PR's body to GitHub (#3122).

    Called by the run loop after a slice PR opens and its number is
    persisted on the contract, so the context PR's slice table gains a
    link to each slice PR as the stack materialises
    (:func:`_compose_context_pr_body` renders ``— #N`` for every slice
    with a recorded ``pr_number``).

    The context PR body is machine-owned: the refresh fully regenerates
    it from contract + pipeline state through the same composer the
    opener used, clobbering any manual edits. Best-effort by design —
    a body refresh is cosmetic, so every failure (contract load,
    composition, gateway) logs a warning and returns ``False`` without
    raising; no slice outcome may depend on it.

    **Concurrency contract**: the caller must hold
    ``get_pipeline_state_lock(pipeline_id)`` for the entire load +
    compose + push sequence — without it, two slices completing in the
    same wave can interleave so the slice whose refresh lands later
    clobbers a body that already included both links. Because no
    later slice fires a refresh after the last one, the final slice's
    ``— #N`` link would stay missing forever if the race fired on it.
    Serializing inside the per-pipeline lock eliminates the race; the
    sole production caller (``_run_implement_phase_slices``) already
    holds it.
    """
    if not pipeline.repo:
        return False

    try:
        from egg_contracts.loader import load_contract

        contract = load_contract(identifier, worktree_repo_path)
    except Exception as load_err:  # noqa: BLE001
        # Lazy import + contract load: ImportError, loader validation
        # errors, OSError on the contract file read.
        _pkg.logger.warning(
            "Context PR body refresh: contract load failed (skipping)",
            pipeline_id=pipeline_id,
            error=str(load_err),
        )
        return False

    context_pr_number = (
        contract.pr.context_pr_number if contract.pr else None
    ) or pipeline.pr_number
    if not context_pr_number:
        # No context PR to refresh — reachable on #3100-degraded
        # contracts where the opener never persisted linkage.
        return False

    try:
        body = _compose_context_pr_body(
            contract=contract,
            pipeline=pipeline,
            worktree_repo_path=worktree_repo_path,
            identifier=identifier,
        )
    except Exception as compose_err:  # noqa: BLE001
        # Pure string composition over loaded state; a raise here is a
        # programming error, but the cosmetic-refresh contract still
        # holds — log and skip rather than fail the slice.
        _pkg.logger.warning(
            "Context PR body refresh: composition failed (skipping)",
            pipeline_id=pipeline_id,
            pr_number=context_pr_number,
            error=str(compose_err),
        )
        return False

    return spawner.gateway.update_pr_body(
        pipeline_id,
        pipeline.repo,
        pr_number=context_pr_number,
        body=body,
        issue_number=pipeline.issue_number,
        # Attribute the action in the gateway audit log; matches
        # sibling orchestrator-driven PR mutations (create_slice_pr,
        # rebase_onto).
        agent_role="orchestrator",
        mode=gateway_mode,
    )


def _open_context_pr_at_implement_start(
    pipeline_id: str, repo_path: Path | None = None
) -> int | None:
    """Hard-required, idempotent up-front context PR opener (#2777, cq-4).

    Single up-front context-PR opener for the plan→implement boundary.
    Replaces the soft-fail ``_maybe_open_base_pr_for_plan_to_implement``
    wrapper (deleted by slice-2 TASK-2-1 in #2777) that swallowed every
    gateway failure with ``return None`` and the four retry-point call
    sites it required. Under the new topology the context PR is
    ``egg/<id>/work → main`` (rather than a dedicated
    ``egg/<id>/context`` branch) and is opened ONCE at the plan→implement
    transition; the slice stack cascades onto it.

    Behaviour:

    1. Look up the pipeline + worktree from ``pipeline_id``.
    2. If the pipeline has neither ``repo`` nor ``base_branch`` set
       (local mode), return ``None`` without raising — there is no
       remote PR to open. This matches the legacy wrapper's silent-skip
       behaviour for local pipelines so the new hard-required contract
       does not regress in-house test pipelines. A ``repo`` with no
       ``base_branch`` is the normal "auto-detect the default branch"
       state (#3031), NOT a misconfiguration: the base is resolved via
       :func:`_detect_default_branch` and used for the lookup + create.
       A ``base_branch`` with no ``repo`` is a genuine misconfiguration
       and raises ``ContextPrCreationError(reason="missing_repo")``.
    3. Otherwise call ``GatewayClient.lookup_open_pr(head, base)`` — the
       same control-plane idempotency primitive ``create_slice_pr`` uses
       — to find the open PR whose head is the pipeline's work branch and
       whose base is the pipeline's base branch. The gateway runs the
       narrow ``gh pr list --head --base --state open`` filter server-side
       (launcher auth, ``/api/v1/gh/find_open_pr``), so both PR-idempotency
       sites share one seam instead of this opener enumerating every open
       PR and filtering client-side (#2934). On hit, persist the PR number
       via :func:`_persist_context_pr_number` and return it (no
       ``gh pr create`` invocation).
    4. On miss, read ``contract.pr.title`` and compose the body via
       :func:`_compose_context_pr_body` (#3115) — the planner's
       ``description`` plus rendered ``test_plan`` / ``manual_steps``
       and a generated pipeline-context footer (issue, slice table,
       analysis/plan draft + BRC transcript links on the work branch).
       Call ``GatewayClient.create_pr`` to open the PR, persist the PR
       number, and return it.

    Raises:
        ContextPrCreationError: on any of (a) pipeline lookup failure,
            (b) contract load failure / missing PR metadata,
            (c) an unexpected ``lookup_open_pr`` failure (the primitive
            itself soft-fails a transient gateway/parse error to ``None``,
            so this only fires on a programming error), (d) ``create_pr``
            failure, (e) persistence failure. NO soft-fail
            ``return None`` for any of these —
            the failure must reach the BRC NACK / 422 surface so the
            operator sees the failure rather than silently stranding
            the slice stack on ``/work``. The test in TASK-3-8 asserts
            no swallow path exists.

    Returns:
        Existing or newly-created PR number on the happy path, OR
        ``None`` ONLY when the pipeline legitimately has no remote
        (local mode). The two outcomes are disambiguated by inspecting
        the pipeline's ``repo`` / ``base_branch`` ahead of the call;
        the run-loop never needs to branch on ``None`` because
        local-mode pipelines never reach the slice loop with remote
        operations queued.

    Idempotency contract:
        Calling the function twice for the same pipeline is safe — the
        second call sees the already-open PR via ``lookup_open_pr`` and
        re-persists the number through :func:`_persist_context_pr_number`.
        No second ``create_pr`` invocation occurs. Tests in TASK-3-8
        verify this by asserting ``create_pr`` is called zero times on
        the idempotent path AND ``_persist_context_pr_number`` IS
        called with the existing PR number.
    """
    # Step 1: resolve the pipeline + worktree path. ``get_state_store_for_pipeline``
    # handles the multi-repo case so the opener works the same way the
    # legacy wrapper did from every call site.
    try:
        from routes import get_state_store_for_pipeline, resolve_worktree_path
    except ImportError as imp_err:
        raise _pkg.ContextPrCreationError(
            "routes helpers unavailable while resolving pipeline",
            reason="routes_unavailable",
            cause=imp_err,
        ) from imp_err

    try:
        store, pipeline = get_state_store_for_pipeline(pipeline_id, repo_path=repo_path)
    except Exception as load_err:
        raise _pkg.ContextPrCreationError(
            f"pipeline {pipeline_id!r} could not be loaded: {load_err}",
            reason="pipeline_load_failed",
            cause=load_err,
        ) from load_err

    # Step 2: local-mode short-circuit + base-branch resolution.
    #
    # ``repo`` AND ``base_branch`` both empty ⇒ local mode (no remote PR
    # to open); return ``None`` without raising.
    #
    # ``repo`` set but ``base_branch`` empty is the NORMAL state, not a
    # misconfiguration: ``Pipeline.base_branch`` defaults to ``None``
    # ("auto-detected from repo's default branch") and the standard
    # ``submit_task`` path never populates it, so essentially every
    # remote pipeline reaches here with ``base_branch=None``. #2777 cq-4
    # collapsed the final ``work → main`` PR into this up-front opener
    # but dropped the default-branch resolution the deleted PR phase did,
    # making the opener the only ``base_branch`` consumer that hard-
    # raised on ``None`` instead of resolving it — stranding every
    # standard pipeline's slice stack on ``/work`` (#3031). Resolve it
    # here the way every other consumer does
    # (``base_branch or _detect_default_branch``) and thread the resolved
    # value through both the idempotency lookup and ``create_pr``.
    #
    # A ``base_branch`` set with no ``repo`` IS a genuine
    # misconfiguration (nothing to open a PR against); surface it as a
    # typed error so the operator notices.
    repo_set = bool(pipeline.repo)
    base_set = bool(pipeline.base_branch)
    if not repo_set and not base_set:
        _pkg.logger.info(
            "Context PR opener: skipping local-mode pipeline (no repo, no base_branch)",
            pipeline_id=pipeline_id,
        )
        return None
    if base_set and not repo_set:
        raise _pkg.ContextPrCreationError(
            f"pipeline {pipeline_id!r} has a base_branch "
            f"({pipeline.base_branch!r}) but no repo; cannot open a context "
            "PR with no remote",
            reason="missing_repo",
        )

    if not pipeline.branch:
        # A remote pipeline without a configured work branch is a
        # structural failure; raise so the operator notices instead of
        # silently skipping (which would re-introduce the soft-fail
        # behaviour cq-4 explicitly removes).
        raise _pkg.ContextPrCreationError(
            f"pipeline {pipeline_id!r} has no branch set; cannot open context PR",
            reason="missing_branch",
        )

    worktree_repo_path = resolve_worktree_path(pipeline_id, store.repo_path)
    # Resolve ``base_branch=None`` to the repo's default branch (#3031).
    # ``_detect_default_branch`` reads ``origin/HEAD`` from the worktree
    # and falls back to ``main``/``master`` then the literal ``"main"``,
    # so it never raises and always yields a concrete base ref for the
    # lookup + create_pr calls below.
    effective_base = pipeline.base_branch or _pkg._detect_default_branch(worktree_repo_path)
    identifier = _pkg._pipeline_identifier(pipeline.issue_number, pipeline_id)
    gateway_mode, _vis = _pkg._compute_gateway_mode(pipeline)

    # Step 3: idempotency pre-flight. Reuse the same control-plane
    # ``lookup_open_pr(head, base)`` primitive the per-slice path
    # (``create_slice_pr``) uses, so both PR-idempotency sites share the
    # narrow server-side ``gh pr list --head --base`` filter on the
    # launcher-auth route rather than this opener enumerating every open
    # PR and filtering client-side (#2934). ``lookup_open_pr`` returns a
    # clean ``int | None`` (the head/base discrimination and number
    # coercion happen server-side + in the primitive), so the client-side
    # match loop and the malformed-``number`` guard the old
    # ``list_open_prs`` path needed are gone. The primitive soft-fails a
    # transient gateway/parse error to ``None`` — matching the slice path,
    # and safe because ``gh pr create`` would reject a duplicate
    # ``head → base`` PR server-side anyway. The ``try`` is the opener's
    # typed-error backstop for an unexpected raise (e.g. a misconfigured
    # gateway client), preserving the cq-4 no-raw-exception contract.
    spawner = _pkg._get_spawner()
    try:
        existing_pr_number = spawner.gateway.lookup_open_pr(
            pipeline_id=pipeline_id,
            repo=pipeline.repo,
            head=pipeline.branch,
            base=effective_base,
        )
    except Exception as lookup_err:
        raise _pkg.ContextPrCreationError(
            f"gateway lookup_open_pr failed for context-PR idempotency check: {lookup_err}",
            reason="lookup_failed",
            cause=lookup_err,
        ) from lookup_err

    if existing_pr_number is not None:
        # Idempotent path. Persist the number even though it MAY
        # already be on the contract: the resume-from-orphaned-pipeline
        # case (contract lost ``context_pr_number`` mid-run) recovers
        # here. The TASK-3-8 unit test asserts the persistence call.
        _pkg._persist_context_pr_number(
            pipeline_id,
            existing_pr_number,
            worktree_repo_path=worktree_repo_path,
            identifier=identifier,
        )
        _pkg.logger.info(
            "Context PR opener: idempotent hit on existing PR (no create_pr call)",
            pipeline_id=pipeline_id,
            pr_number=existing_pr_number,
            head=pipeline.branch,
            base=effective_base,
        )
        _maybe_open_secondary_context_prs(
            pipeline_id,
            pipeline=pipeline,
            primary_pr_number=existing_pr_number,
            work_branch=pipeline.branch,
            worktree_repo_path=worktree_repo_path,
            identifier=identifier,
            gateway_mode=gateway_mode,
            spawner=spawner,
        )
        return existing_pr_number

    # Step 4: open a new context PR. Read title/description from the
    # canonical ``contract.pr`` fields (populated from the plan's
    # ``pr:`` block by ``_populate_contract_from_plan``).
    try:
        from egg_contracts.loader import load_contract
    except ImportError as imp_err:
        raise _pkg.ContextPrCreationError(
            "egg_contracts.loader unavailable while reading PR metadata",
            reason="loader_unavailable",
            cause=imp_err,
        ) from imp_err

    try:
        contract = load_contract(identifier, worktree_repo_path)
    except Exception as load_err:
        raise _pkg.ContextPrCreationError(
            f"failed to load contract for {identifier!r}: {load_err}",
            reason="contract_load_failed",
            cause=load_err,
        ) from load_err

    if contract.pr is None or not (contract.pr.title or "").strip():
        raise _pkg.ContextPrCreationError(
            "contract.pr.title is missing or empty; cannot open context PR",
            reason="missing_pr_metadata",
        )
    pr_title = contract.pr.title.strip()
    # #3115: render the full context-PR body (description + test plan +
    # manual steps + generated pipeline-context footer) instead of the
    # bare ``contract.pr.description``.
    pr_body = _compose_context_pr_body(
        contract=contract,
        pipeline=pipeline,
        worktree_repo_path=worktree_repo_path,
        identifier=identifier,
    )

    try:
        pr_url = spawner.gateway.create_pr(
            pipeline_id=pipeline_id,
            repo=pipeline.repo,
            title=pr_title,
            body=pr_body,
            head=pipeline.branch,
            base=effective_base,
            issue_number=pipeline.issue_number,
            mode=gateway_mode,
        )
    except Exception as create_err:
        raise _pkg.ContextPrCreationError(
            f"gateway create_pr failed for context PR: {create_err}",
            reason="gateway_error",
            cause=create_err,
        ) from create_err

    if not pr_url:
        raise _pkg.ContextPrCreationError(
            "gateway create_pr returned no URL; cannot derive context PR number",
            reason="gateway_no_url",
        )

    # Extract the PR number from the URL — gh prints
    # ``https://github.com/<owner>/<repo>/pull/<N>`` on stdout.
    # Use a trailing-boundary pattern (end-of-string OR a non-digit
    # path/query separator) so that a hypothetical
    # ``/pull/12345/files`` or ``/pull/12345?diff=split`` URL still
    # parses correctly but a digit-suffixed slug like
    # ``/pulled-files/12345`` cannot smuggle a wrong number through
    # (reviewer_concurrency non-blocking #2 hardening).
    match = re.search(r"/pull/(\d+)(?:[/?#]|$)", pr_url)
    if not match:
        raise _pkg.ContextPrCreationError(
            f"could not parse PR number from create_pr URL: {pr_url!r}",
            reason="gateway_bad_url",
        )
    try:
        new_pr_number = int(match.group(1))
    except (TypeError, ValueError) as parse_err:
        raise _pkg.ContextPrCreationError(
            f"could not coerce PR number from create_pr URL: {pr_url!r}",
            reason="gateway_bad_url",
            cause=parse_err,
        ) from parse_err

    _pkg._persist_context_pr_number(
        pipeline_id,
        new_pr_number,
        worktree_repo_path=worktree_repo_path,
        identifier=identifier,
        pr_url=pr_url,
    )

    _pkg.logger.info(
        "Context PR opener: opened new PR at plan→implement boundary (#2777)",
        pipeline_id=pipeline_id,
        pr_number=new_pr_number,
        head=pipeline.branch,
        base=effective_base,
        url=pr_url,
    )
    _maybe_open_secondary_context_prs(
        pipeline_id,
        pipeline=pipeline,
        primary_pr_number=new_pr_number,
        work_branch=pipeline.branch,
        worktree_repo_path=worktree_repo_path,
        identifier=identifier,
        gateway_mode=gateway_mode,
        spawner=spawner,
    )
    return new_pr_number


def _repos_with_slices(contract, pipeline) -> list[str]:
    """Repos that own ≥1 slice — the lazy-per-repo participation set (#3393, slice-4).

    A repo *participates* (gets its own ``egg/<id>/work`` branch + context
    PR) iff at least one slice resolves to it via
    :func:`models.resolve_slice_repo`. The result is ordered by
    ``pipeline.repos`` and de-duplicated; a submitted repo that ends up
    owning no slices is excluded (operator ruling #1). For an N=1 pipeline
    this returns the single repo. This is the invariant the context-PR
    opener's per-repo iteration honours (task-4-2).
    """
    try:
        from models import resolve_slice_repo  # type: ignore[no-redef]
    except ImportError:
        from ..models import resolve_slice_repo  # type: ignore[no-redef]

    slices = getattr(contract, "slices", None) or []
    owning = {resolve_slice_repo(s, pipeline) for s in slices}
    return [spec.repo for spec in (pipeline.repos or []) if spec.repo in owning]


def _maybe_open_secondary_context_prs(
    pipeline_id: str,
    *,
    pipeline: Any,
    primary_pr_number: int,
    work_branch: str | None,
    worktree_repo_path: Path,
    identifier: int | str,
    gateway_mode: str,
    spawner: Any,
) -> None:
    """Guarded, never-raising entry to the lazy per-repo context opener (#3393).

    No-op unless the pipeline coordinates more than one repo, so the N=1
    single-repo path in :func:`_open_context_pr_at_implement_start`
    performs zero extra work (no contract load, no gateway calls) and is
    byte-for-byte unchanged. Requires a resolvable primary repo + work
    branch; both are guaranteed set on the multi-repo remote path that
    reaches here (the opener already returned for local-mode pipelines).
    """
    if len(getattr(pipeline, "repos", None) or []) <= 1:
        return
    primary_repo = pipeline.primary_repo
    if not primary_repo or not work_branch:
        return
    try:
        _open_secondary_context_prs(
            pipeline_id,
            pipeline=pipeline,
            primary_repo=primary_repo,
            primary_pr_number=primary_pr_number,
            work_branch=work_branch,
            worktree_repo_path=worktree_repo_path,
            identifier=identifier,
            gateway_mode=gateway_mode,
            spawner=spawner,
        )
    except Exception as sec_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Lazy per-repo context PRs raised (continuing — primary context PR unaffected) (#3393)",
            pipeline_id=pipeline_id,
            error=str(sec_err),
        )


def _open_secondary_context_prs(
    pipeline_id: str,
    *,
    pipeline: Any,
    primary_repo: str,
    primary_pr_number: int,
    work_branch: str,
    worktree_repo_path: Path,
    identifier: int | str,
    gateway_mode: str,
    spawner: Any,
) -> dict[str, int]:
    """Open the lazy per-repo context PRs for a multi-repo pipeline (#3393, slice-4 / task-4-2).

    :func:`_open_context_pr_at_implement_start` opens the PRIMARY repo's
    context PR (``egg/<id>/work → base``) exactly as it always has. This
    helper adds the *other* repos: it iterates the set of repos that own
    ≥1 slice (via ``resolve_slice_repo`` over the contract's slices),
    drops the primary, and for each remaining repo opens that repo's own
    ``egg/<id>/work`` context PR (same branch naming, per repo). A
    submitted repo with NO slices is skipped — lazy-per-repo, operator
    ruling #1. Every opened context PR (primary + secondaries) then has
    its body refreshed to cross-reference the sibling context PRs in the
    other repos (``## Coordinated repos``).

    It is only invoked when ``len(pipeline.repos) > 1``; for an N=1
    pipeline the caller never reaches here, so the single-repo path is
    byte-for-byte unchanged.

    Prerequisite / current limit (honest scope note): opening a context
    PR in a secondary repo requires that repo's ``egg/<id>/work`` branch
    to exist on its remote, which in turn needs a secondary-repo worktree
    to push it. Threading the full repo set into worktree CREATION was
    explicitly deferred by slice-3 (the worktree map is owner/repo-keyed
    and list-shaped, but only the primary repo is materialised today), so
    until that later wiring lands the secondary ``create_pr`` will
    typically fail on a missing head branch. This helper therefore:

    * uses the launcher-auth ``lookup_open_pr`` idempotency primitive
      (which works per-repo with no worktree) to ADOPT an already-open
      secondary context PR, and
    * ATTEMPTS ``create_pr`` otherwise, soft-failing (log, continue) so a
      missing secondary branch never strands the pipeline.

    The iteration + cross-referencing structure is therefore complete and
    forward-compatible: once secondary-repo worktree/branch creation is
    wired, secondary context PRs open with no further change here.

    Every failure is caught and logged; the helper never raises. Returns
    the ``{repo: pr_number}`` map of context PRs known after the pass
    (always including the primary), for logging / tests.
    """
    opened: dict[str, int] = {primary_repo: primary_pr_number}

    try:
        from egg_contracts.loader import load_contract
    except ImportError:
        _pkg.logger.warning(
            "Secondary context PRs: egg_contracts.loader unavailable (skipping) (#3393)",
            pipeline_id=pipeline_id,
        )
        return opened

    try:
        contract = load_contract(identifier, worktree_repo_path)
    except Exception as load_err:  # noqa: BLE001
        _pkg.logger.warning(
            "Secondary context PRs: contract load failed (skipping) (#3393)",
            pipeline_id=pipeline_id,
            error=str(load_err),
        )
        return opened

    # Repos owning ≥1 slice (ordered by ``pipeline.repos``), minus the
    # primary — the lazy-per-repo participation set (task-4-2).
    secondary_repos = [r for r in _repos_with_slices(contract, pipeline) if r != primary_repo]

    if not secondary_repos:
        # Multi-repo pipeline whose slices all resolve to the primary
        # (e.g. no slice pinned a secondary repo). Nothing lazy to open.
        return opened

    base_by_repo = {spec.repo: spec.base_branch for spec in (pipeline.repos or [])}
    context_pr_title = (
        contract.pr.title.strip()
        if contract.pr and (contract.pr.title or "").strip()
        else f"{identifier} context"
    )

    for repo in secondary_repos:
        # ``base_branch=None`` ⇒ the repo's default branch. Without a
        # secondary worktree we cannot run ``_detect_default_branch``
        # here, so fall back to ``main`` (the create call resolves the
        # real default server-side when base is omitted anyway).
        base = base_by_repo.get(repo) or "main"
        try:
            existing = spawner.gateway.lookup_open_pr(
                pipeline_id=pipeline_id,
                repo=repo,
                head=work_branch,
                base=base,
            )
            if existing is not None:
                opened[repo] = existing
                _pkg.logger.info(
                    "Secondary context PR: adopted existing PR (#3393)",
                    pipeline_id=pipeline_id,
                    repo=repo,
                    pr_number=existing,
                )
                continue

            body = _compose_context_pr_body(
                contract=contract,
                pipeline=pipeline,
                worktree_repo_path=worktree_repo_path,
                identifier=identifier,
                context_repo=repo,
                sibling_context_prs=[
                    {"repo": r, "number": n} for r, n in opened.items() if r != repo
                ],
            )
            pr_url = spawner.gateway.create_pr(
                pipeline_id=pipeline_id,
                repo=repo,
                title=context_pr_title,
                body=body,
                head=work_branch,
                base=base,
                issue_number=pipeline.issue_number,
                mode=gateway_mode,  # type: ignore[arg-type]
            )
            match = re.search(r"/pull/(\d+)(?:[/?#]|$)", pr_url or "")
            if match:
                opened[repo] = int(match.group(1))
                _pkg.logger.info(
                    "Secondary context PR: opened new PR (#3393)",
                    pipeline_id=pipeline_id,
                    repo=repo,
                    pr_number=opened[repo],
                    head=work_branch,
                    base=base,
                )
            else:
                _pkg.logger.warning(
                    "Secondary context PR: create returned no parseable URL (#3393)",
                    pipeline_id=pipeline_id,
                    repo=repo,
                    url=pr_url,
                )
        except Exception as sec_err:  # noqa: BLE001
            # Best-effort: a missing secondary ``egg/<id>/work`` branch
            # (the deferred-worktree limit above) surfaces here as a
            # gateway create failure. Log + continue so the primary
            # context PR + slice stack are unaffected.
            _pkg.logger.warning(
                "Secondary context PR deferred (continuing) — secondary-repo "
                "work branch likely absent until secondary worktree creation "
                "is wired (#3393)",
                pipeline_id=pipeline_id,
                repo=repo,
                error=str(sec_err),
            )

    # Cross-reference pass: refresh every opened context PR body so each
    # links the sibling context PRs in the other repos. Best-effort and
    # cosmetic — a failed refresh never affects the slice stack.
    if len(opened) > 1:
        for repo, number in opened.items():
            try:
                body = _compose_context_pr_body(
                    contract=contract,
                    pipeline=pipeline,
                    worktree_repo_path=worktree_repo_path,
                    identifier=identifier,
                    context_repo=repo,
                    sibling_context_prs=[
                        {"repo": r, "number": n} for r, n in opened.items() if r != repo
                    ],
                )
                spawner.gateway.update_pr_body(
                    pipeline_id=pipeline_id,
                    repo=repo,
                    pr_number=number,
                    body=body,
                    issue_number=pipeline.issue_number,
                    mode=gateway_mode,  # type: ignore[arg-type]
                )
            except Exception as refresh_err:  # noqa: BLE001
                _pkg.logger.warning(
                    "Coordinated-repos cross-reference refresh failed (continuing) (#3393)",
                    pipeline_id=pipeline_id,
                    repo=repo,
                    error=str(refresh_err),
                )

    return opened
