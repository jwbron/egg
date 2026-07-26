"""brc history helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim; patched/barrel-resident globals reached via _pkg so
patch("routes.pipelines.<name>") keeps intercepting.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import routes.pipelines as _pkg  # noqa: E402,F401

if TYPE_CHECKING:
    try:
        from ..container_spawner import ContainerSpawner  # noqa: F401
    except ImportError:  # pragma: no cover
        from container_spawner import ContainerSpawner  # type: ignore  # noqa: F401

import yaml
from models import Pipeline, PipelineStatus
from slice_id_validation import SLICE_ID_PATTERN
from state_store import StateStore

BRC_HISTORY_TYPES = frozenset(
    {
        "CONSENSUS_PROPOSE",
        "CONSENSUS_ACK",
        "CONSENSUS_NACK",
        "CONSENSUS_WITHDRAW",
        "CONSENSUS_CONFIRMED",
        "CONSENSUS_RE_REVIEW",
        # In-cycle conditional-ACK obligation resolution (#2338). Captured
        # in the BRC history file so the audit trail survives orchestrator
        # teardown — closes the gap that resolution was previously only
        # an in-memory event.
        "CONSENSUS_OBLIGATION_RESOLVED",
        "STATUS",
        "HANDOFF",
        "AGENT_FAILED",
        "NUDGE",
        "OVERSEER_ALERT",
        # HEARTBEAT (issue #1897) — structured per-agent state messages.
        "HEARTBEAT",
        # QUESTION removed per issue #1897 Phase 7.  The enum member
        # remains for backward-compat until the tester updates
        # test_brc_history / test_checkpoint fixtures; see
        # MessageType.QUESTION.
    }
)


CONSENSUS_BRC_TYPES = frozenset(
    {
        "CONSENSUS_PROPOSE",
        "CONSENSUS_ACK",
        "CONSENSUS_NACK",
        "CONSENSUS_WITHDRAW",
        "CONSENSUS_CONFIRMED",
        "CONSENSUS_RE_REVIEW",
        "CONSENSUS_OBLIGATION_RESOLVED",
    }
)


def _get_message_store():
    """Import and return the message store factory function, or None if unavailable."""
    try:
        from message_store import get_message_store
    except ImportError:
        try:
            from ..message_store import get_message_store  # type: ignore[import-not-found]
        except ImportError:
            return None
    return get_message_store


def _render_brc_history_markdown(
    brc_messages: list[Any],
    pipeline_id: str,
    phase: str,
    *,
    slice_id: str | None = None,
) -> str:
    """Render *brc_messages* as a chronological markdown log.

    The output shape mirrors the legacy aggregate file: a heading line,
    a generated-timestamp footer, and one ``### [ts] role (TYPE): subject``
    section per message with a fenced YAML metadata block.

    ``Generated:`` is derived from the *latest* message timestamp (not
    wall-clock time) so regenerating the file from the same message set
    produces byte-identical output.  This keeps the PR-phase safety-net
    rewrite (:func:`_rewrite_brc_history_for_pr`) idempotent: when no new
    BRC messages arrived between phase completion and PR creation, the
    rewritten file matches the previous commit and the follow-up commit is
    skipped by :func:`_commit_statefiles_to_worktree`.  See #1714.
    """
    message_timestamps = [m.timestamp for m in brc_messages if m.timestamp is not None]
    if message_timestamps:
        generated_str = max(message_timestamps).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        generated_str = "unknown"
    # The "unattributed" bucket is not a slice — it holds cross-cutting
    # non-CONSENSUS messages that lack canonical slice scope (HEARTBEAT,
    # OVERSEER_ALERT, AGENT_FAILED, …) routed to a sibling file so the
    # audit trail stays complete. Rendering it as "Slice: unattributed"
    # would mislead a reviewer who lands on the file via a link line —
    # special-case the heading and metadata block instead.
    is_unattributed = slice_id == "unattributed"
    lines: list[str] = []
    if is_unattributed:
        lines.append(f"# BRC Consensus History — {phase} phase, cross-cutting (unattributed)")
    elif slice_id:
        lines.append(f"# BRC Consensus History — {phase} phase, {slice_id}")
    else:
        lines.append(f"# BRC Consensus History — {phase} phase")
    lines.append("")
    lines.append(f"Generated: {generated_str}")
    lines.append(f"Pipeline: {pipeline_id}")
    if is_unattributed:
        lines.append("Section: cross-cutting (unattributed)")
    elif slice_id:
        lines.append(f"Slice: {slice_id}")
    lines.append("")

    for msg in brc_messages:
        ts = msg.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if msg.timestamp else "unknown"
        # Include to_role for directed messages (not broadcast "all")
        if msg.to_role and msg.to_role != "all":
            header = (
                f"### [{ts}] {msg.from_role} → {msg.to_role} ({msg.message_type}): {msg.subject}"
            )
        else:
            header = f"### [{ts}] {msg.from_role} ({msg.message_type}): {msg.subject}"
        lines.append(header)
        if msg.body:
            lines.append("")
            lines.append(msg.body)

        # Emit a YAML metadata block with id, phase, and non-empty metadata
        meta_block: dict[str, Any] = {}
        if msg.id:
            meta_block["id"] = msg.id
        if msg.phase:
            meta_block["phase"] = msg.phase
        if msg.metadata:
            meta_block["metadata"] = msg.metadata
        if meta_block:
            lines.append("")
            lines.append("````yaml")
            lines.append(
                yaml.safe_dump(meta_block, sort_keys=False, default_flow_style=False).rstrip()
            )
            lines.append("````")
        lines.append("")
    return "\n".join(lines)


def _write_brc_history_file(
    worktree_path: Path,
    pipeline_id: str,
    phase: str,
    identifier: int | str,
    brc_messages: list[Any],
    *,
    slice_id: str | None = None,
) -> None:
    """Render and persist the markdown + JSON companion files for one bucket.

    ``slice_id``, when provided, switches the on-disk filename from the
    aggregate ``{identifier}-{phase}.{md,json}`` shape used by
    refine/plan/pr to the per-slice ``{identifier}-{phase}-{slice_id}.{md,json}``
    shape used by implement (#2548 — hard switchover, no aggregate
    implement file is produced).
    """
    if slice_id:
        stem = f"{identifier}-{phase}-{slice_id}"
    else:
        stem = f"{identifier}-{phase}"

    history_dir = worktree_path / ".egg-state" / "brc-history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"{stem}.md"

    # Write the markdown history file
    try:
        history_file.write_text(
            _render_brc_history_markdown(
                brc_messages,
                pipeline_id,
                phase,
                slice_id=slice_id,
            )
        )
    except Exception as md_err:
        _pkg.logger.warning(
            "Failed to write BRC history markdown file",
            pipeline_id=pipeline_id,
            phase=phase,
            slice_id=slice_id,
            error=str(md_err),
        )

    # Write a JSON companion artifact containing the full message dicts
    json_file = history_dir / f"{stem}.json"
    try:
        json_data = [msg.to_dict() for msg in brc_messages]
        json_file.write_text(json.dumps(json_data, indent=2, default=str))
    except Exception as json_err:
        _pkg.logger.warning(
            "Failed to write BRC history JSON companion file",
            pipeline_id=pipeline_id,
            phase=phase,
            slice_id=slice_id,
            error=str(json_err),
        )

    _pkg.logger.info(
        "Wrote BRC history file",
        pipeline_id=pipeline_id,
        phase=phase,
        slice_id=slice_id,
        path=str(history_file),
        message_count=len(brc_messages),
    )


def _write_brc_history(
    worktree_path: Path,
    pipeline_id: str,
    phase: str,
    identifier: int | str,
    *,
    write_per_slice: bool = True,
    run_epoch: str | None = None,
) -> None:
    """Write BRC consensus message history for a phase to .egg-state.

    Retrieves BRC-related messages for the given phase from the message store
    and writes them as a chronological markdown log to
    ``.egg-state/brc-history/{identifier}-{phase}.md``.

    For the ``implement`` phase the writer auto-detects slice-aware vs
    aggregate mode (#2548):

    * If at least one BRC message carries a canonical
      ``metadata['slice_id']`` (validated against
      ``SLICE_ID_PATTERN``), the writer partitions messages per-slice
      and writes one file per slice as
      ``{identifier}-implement-{slice_id}.{md,json}``.
      Per-message attribution rules:

      - ``CONSENSUS_*`` messages without a canonical slice_id are
        dropped with a single aggregate WARNING — the orchestrator's
        CONSENSUS_* signal handlers tag every implement-phase write
        under D4, so a missing slice_id is a contract violation.
      - Other ``BRC_HISTORY_TYPES`` (HEARTBEAT, STATUS, HANDOFF,
        AGENT_FAILED, NUDGE, OVERSEER_ALERT) come from emitters that
        do not uniformly carry slice scope. When they lack a
        canonical slice_id they are routed to a sibling
        ``{identifier}-implement-unattributed.{md,json}`` file rather
        than dropped, so the audit trail stays complete and reviewers
        of any per-slice transcript can cross-reference.

    * If **no** BRC message carries a slice_id (non-slice pipelines),
      the writer falls back to the aggregate
      ``{identifier}-implement.{md,json}`` filename.

    No-ops gracefully when the message store is unavailable or contains no
    BRC messages for the pipeline and phase.

    Args:
        worktree_path: Path to the worktree repo directory
        pipeline_id: The pipeline ID to retrieve messages for
        phase: The pipeline phase name (e.g. "implement", "plan")
        identifier: The pipeline identifier for file naming
        write_per_slice: When False and ``phase == "implement"`` in a
            slice-aware pipeline, skip writing the per-slice
            ``{identifier}-implement-{slice_id}.{md,json}`` files. The
            ``unattributed`` sibling and any non-slice aggregate file
            are still written. Per-slice files are owned by their
            slice's integration branch (committed by
            :func:`_commit_slice_brc_history_to_integration_branch`);
            duplicating them onto ``work`` causes add/add merge
            conflicts when slice PRs target ``work`` (#2755). Default
            ``True`` preserves the historical behavior for the slice
            hook itself and for any out-of-tree callers.
    """
    _pkg.logger.info(
        "_write_brc_history: entering",
        pipeline_id=pipeline_id,
        phase=phase,
        identifier=str(identifier),
    )

    store_fn = _pkg._get_message_store()
    if store_fn is None:
        _pkg.logger.info(
            "_write_brc_history: early return — message store unavailable",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return

    try:
        store = store_fn()
        messages = store.get_messages(pipeline_id, limit=10000, run_epoch=run_epoch)
    except Exception as e:
        _pkg.logger.warning(
            "_write_brc_history: early return — failed to retrieve messages",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(e),
        )
        return

    if not messages:
        _pkg.logger.info(
            "_write_brc_history: early return — no messages in store",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return

    brc_messages = [m for m in messages if m.message_type in BRC_HISTORY_TYPES and m.phase == phase]
    if not brc_messages:
        _pkg.logger.info(
            "_write_brc_history: early return — no BRC messages for phase",
            pipeline_id=pipeline_id,
            phase=phase,
            total_messages=len(messages),
        )
        return

    if phase == "implement":
        # Implement-phase BRC messages are partitioned per-slice (#2548)
        # for slice-aware pipelines (issue mode with `contract.slices`):
        # the orchestrator's CONSENSUS_* signal handlers tag every
        # implement-phase consensus message with `metadata['slice_id']`,
        # and this writer buckets them into one transcript file per
        # slice. Non-slice pipelines have no slice scope on any message,
        # so they fall back to the aggregate
        # `{identifier}-implement.{md,json}` filename.
        #
        # ``metadata['slice_id']`` is interpolated into the on-disk
        # filename below, so this is a gateway-facing seam in the same
        # sense as ``signals.py`` / the restart route /
        # ``concurrent_executor`` branch builders: every value MUST be
        # validated against the canonical ``SLICE_ID_PATTERN`` before
        # use, otherwise an attacker-controlled metadata blob (any role
        # can post arbitrary metadata via ``messages.py``) could smuggle
        # path separators into the filename and write outside
        # ``.egg-state/brc-history/``. See ``slice_id_validation.py``
        # for the invariant. ``SLICE_ID_PATTERN`` is already imported at
        # module top (the same try/except sandbox-vs-orchestrator dual
        # import that imports ``extract_slice_id``); no local re-import
        # is needed.

        buckets: dict[str, list[Any]] = {}
        # ``unattributed_consensus`` holds CONSENSUS_* messages that lack
        # a canonical slice_id — those are a D4 contract violation and
        # are dropped with a single aggregate WARNING. ``unattributed_other``
        # holds non-CONSENSUS BRC types (HEARTBEAT, STATUS, HANDOFF,
        # AGENT_FAILED, NUDGE, OVERSEER_ALERT) whose emitters do not
        # uniformly carry slice scope; those are written to the
        # ``unattributed`` sibling file so the audit trail stays complete.
        unattributed_consensus: list[Any] = []
        unattributed_other: list[Any] = []
        for msg in brc_messages:
            # ``Message.metadata`` is a Pydantic dict[str, Any] field with a
            # default_factory=dict (message_store.Message), so it is always a
            # dict at this point — no need to guard with getattr/isinstance.
            raw_slice_id = msg.metadata.get("slice_id")
            if isinstance(raw_slice_id, str) and SLICE_ID_PATTERN.fullmatch(raw_slice_id):
                buckets.setdefault(raw_slice_id, []).append(msg)
                continue
            if str(getattr(msg, "message_type", "")) in CONSENSUS_BRC_TYPES:
                unattributed_consensus.append(msg)
            else:
                unattributed_other.append(msg)

        if not buckets:
            # No slice-attributed messages anywhere — this is a non-slice
            # pipeline (an implement-phase run that never spawned slice
            # scopes). Fall back to the aggregate
            # `{identifier}-implement.{md,json}` filename so we never
            # silently drop the entire BRC stream when no per-slice
            # bucketing is possible. See #2548 reviewer_code_holistic
            # finding #3.
            _pkg.logger.info(
                "_write_brc_history: no slice-attributed implement-phase "
                "messages — writing aggregate file (non-slice pipeline)",
                pipeline_id=pipeline_id,
                phase=phase,
                total_brc_messages=len(brc_messages),
            )
            _write_brc_history_file(
                worktree_path,
                pipeline_id,
                phase,
                identifier,
                brc_messages,
            )
            return

        # Slice-aware pipeline: at least one canonical slice_id was
        # observed. CONSENSUS_* messages that lack a canonical slice_id
        # are a D4 hard-switchover contract violation — drop them with
        # a loud aggregate WARNING (count + sample types) so an operator
        # notices the asymmetry rather than silently shipping a thinned-
        # out transcript.
        if unattributed_consensus:
            sample_types = sorted(
                {str(getattr(m, "message_type", "")) for m in unattributed_consensus[:8]}
            )
            _pkg.logger.warning(
                "_write_brc_history: dropped implement-phase CONSENSUS_* messages "
                "without canonical metadata.slice_id (hard switchover, #2548)",
                pipeline_id=pipeline_id,
                phase=phase,
                dropped_count=len(unattributed_consensus),
                sample_message_types=sample_types,
                attributed_count=sum(len(v) for v in buckets.values()),
            )

        # Non-CONSENSUS BRC types without a canonical slice_id come from
        # emitters that do not uniformly attach slice scope (HealthMonitor
        # nudges, overseer respawn alerts, AGENT_FAILED broadcasts,
        # CLI-routed HANDOFF/NUDGE messages, etc.). Route them to a
        # sibling ``{identifier}-implement-unattributed.{md,json}`` file
        # so the audit trail stays complete — reviewers reading any
        # per-slice transcript can cross-reference. See #2548
        # reviewer_code blocking finding.
        if unattributed_other:
            _write_brc_history_file(
                worktree_path,
                pipeline_id,
                phase,
                identifier,
                unattributed_other,
                slice_id="unattributed",
            )

        if not write_per_slice:
            # Caller opted out of per-slice writes (#2755). The
            # ``unattributed`` sibling has already been written above
            # (when ``unattributed_other`` was non-empty); skip the
            # per-slice bucket loop so we don't add files that the
            # slice branches already own. See the docstring's
            # ``write_per_slice`` arg for the merge-conflict rationale.
            _pkg.logger.info(
                "_write_brc_history: skipping per-slice writes (write_per_slice=False)",
                pipeline_id=pipeline_id,
                phase=phase,
                slice_bucket_count=len(buckets),
            )
            return

        # Natural sort by the integer suffix so a 12-slice pipeline iterates
        # `slice-1, slice-2, ..., slice-12` rather than the lexicographic
        # `slice-1, slice-10, slice-11, slice-12, slice-2`. Every key is
        # already SLICE_ID_PATTERN-validated (`^slice-[0-9]+$`) above, so the
        # int() parse is total.
        for slice_id, slice_msgs in sorted(
            buckets.items(), key=lambda kv: int(kv[0].rsplit("-", 1)[1])
        ):
            _write_brc_history_file(
                worktree_path,
                pipeline_id,
                phase,
                identifier,
                slice_msgs,
                slice_id=slice_id,
            )
        return

    # Refine, plan, and pr phases continue to write the aggregate
    # `{identifier}-{phase}.{md,json}` file — only implement is per-slice.
    _write_brc_history_file(
        worktree_path,
        pipeline_id,
        phase,
        identifier,
        brc_messages,
    )


def _rewrite_brc_history_for_pr(
    worktree_path: Path,
    pipeline_id: str,
    pipeline_phases: dict,
    identifier: int | str,
) -> None:
    """Re-write BRC history for all completed phases before PR creation.

    Iterates ``pipeline_phases`` (a mapping of phase name → phase execution
    objects with a ``.status`` attribute) and calls :func:`_write_brc_history`
    for each phase whose status is ``PipelineStatus.COMPLETE``.

    Errors from individual phase writes are logged at warning level and
    do not prevent other phases from being processed.

    After re-writing history files, commits the results via
    :func:`_commit_statefiles_to_worktree`.  Commit failures are also
    logged and swallowed so the PR creation can proceed.
    """
    completed_phases = [
        name for name, ex in pipeline_phases.items() if ex.status == PipelineStatus.COMPLETE
    ]
    _pkg.logger.info(
        "_rewrite_brc_history_for_pr: entering",
        pipeline_id=pipeline_id,
        total_phases=len(pipeline_phases),
        completed_phase_count=len(completed_phases),
        completed_phases=completed_phases,
    )
    for phase_name, phase_exec in pipeline_phases.items():
        if phase_exec.status == PipelineStatus.COMPLETE:
            try:
                _pkg._write_brc_history(
                    worktree_path,
                    pipeline_id,
                    phase_name,
                    identifier,
                    # Per-slice implement-phase files are owned by each
                    # slice's integration branch (#2548 D2/D5); committing
                    # them onto ``work`` would re-introduce the add/add
                    # merge conflict from #2755. Only the aggregate /
                    # unattributed sibling lands on ``work``.
                    write_per_slice=False,
                )
            except Exception as brc_err:
                _pkg.logger.warning(
                    "Failed to re-write BRC history for PR (continuing)",
                    pipeline_id=pipeline_id,
                    phase=phase_name,
                    error=str(brc_err),
                )
    try:
        _pkg._commit_statefiles_to_worktree(
            worktree_path,
            "Persist BRC history files for PR",
            pipeline_identifier=identifier,
            pipeline_id=pipeline_id,
        )
        _pkg.logger.info(
            "_rewrite_brc_history_for_pr: commit step completed successfully",
            pipeline_id=pipeline_id,
        )
    except subprocess.CalledProcessError as git_err:
        _pkg.logger.warning(
            "Failed to commit BRC history for PR (continuing)",
            pipeline_id=pipeline_id,
            error=str(git_err),
        )
    _pkg.logger.info(
        "_rewrite_brc_history_for_pr: exiting",
        pipeline_id=pipeline_id,
    )


def _persist_phase_brc_history(
    pipeline: Pipeline,
    store: StateStore,
    phase: str,
) -> None:
    """Persist BRC history for *phase* and commit it, best-effort.

    Mirrors the per-phase write+commit sequence that ``_run_pipeline``
    runs inline at phase completion, so external phase-transition paths
    (the ``complete_phase`` / ``advance_phase`` REST+MCP handlers) do
    not silently drop BRC transcripts when ``_clear_concurrent_state``
    wipes the message store.  See #1827.

    Note: this commits but does **not** push.  Callers must ensure a
    push happens downstream — in ``advance_phase`` the spawned
    ``_run_pipeline`` thread pushes the branch, carrying this commit
    along; in a standalone ``complete_phase`` the caller is expected to
    trigger a subsequent advance or push.
    """
    worktree_path = _pkg._resolve_pipeline_worktree_path(pipeline, store.repo_path)
    _run_epoch_str = pipeline.run_epoch.isoformat() if pipeline.run_epoch else None
    try:
        _pkg._write_brc_history(
            worktree_path,
            pipeline.id,
            phase,
            _pkg._brc_history_identifier(pipeline),
            # Per-slice implement-phase files are owned by the slice's
            # integration branch (committed by
            # :func:`_commit_slice_brc_history_to_integration_branch`);
            # the work-branch worktree must not duplicate them, otherwise
            # slice PRs targeting ``work`` hit add/add merge conflicts
            # (#2755). The parameter is a no-op for non-implement phases.
            write_per_slice=False,
            run_epoch=_run_epoch_str,
        )
    except Exception as brc_err:
        _pkg.logger.warning(
            "Failed to persist BRC history before phase transition (continuing)",
            pipeline_id=pipeline.id,
            phase=phase,
            error=str(brc_err),
        )
        return

    try:
        _pkg._commit_statefiles_to_worktree(
            worktree_path,
            f"Persist statefiles after {phase} phase",
            pipeline_identifier=_pkg._pipeline_identifier(pipeline.issue_number, pipeline.id),
            # Contract files are keyed by pipeline_id, not the issue-number
            # prefix; without this the restart-time persist skipped the
            # contract entirely (#1829 gap, observed in #3427).
            pipeline_id=pipeline.id,
        )
    except subprocess.CalledProcessError as git_err:
        _pkg.logger.warning(
            "Failed to commit BRC history before phase transition (continuing)",
            pipeline_id=pipeline.id,
            phase=phase,
            error=str(git_err),
        )


def _persist_cancel_brc_history(
    pipeline: Pipeline,
    store: StateStore,
) -> None:
    """Persist BRC history for the in-flight phase on cancel, best-effort.

    Called from the cancel path in ``_routes_crud.py`` before
    ``_clear_pipeline_runtime_state`` runs for FAILED (cancel does not
    clear runtime state — #3632 Change 1). Unlike
    :func:`_persist_phase_brc_history` (which writes with
    ``write_per_slice=False`` to avoid add/add conflicts on slice PRs
    targeting ``work``), this function writes with
    ``write_per_slice=True`` so the in-flight slice's per-slice
    CONSENSUS_* buckets are persisted to disk. The cancel path writes
    to the pipeline's worktree (not a slice PR), so there is no
    add/add conflict risk.

    This is the #3632 Change 3 fix: without it, the in-flight slice's
    BRC evidence is lost when the message store is eventually cleared
    (on a future phase transition or pipeline delete), making the
    forensic record for the slice that was most likely being paused
    for investigation simply gone.

    Never raises — cancel must not fail because BRC history persistence
    failed. Logs a warning on failure.
    """
    _run_epoch_str = pipeline.run_epoch.isoformat() if pipeline.run_epoch else None
    current_phase = pipeline.current_phase.value if pipeline.current_phase else None
    if current_phase is None:
        _pkg.logger.info(
            "_persist_cancel_brc_history: no current phase, skipping",
            pipeline_id=pipeline.id,
        )
        return

    worktree_path = _pkg._resolve_pipeline_worktree_path(pipeline, store.repo_path)
    try:
        _pkg._write_brc_history(
            worktree_path,
            pipeline.id,
            current_phase,
            _pkg._brc_history_identifier(pipeline),
            # write_per_slice=True so per-slice CONSENSUS_* buckets are
            # written — this is the gap that made the previous incident's
            # in-flight slice unrecoverable (restart_phase used
            # write_per_slice=False). The cancel path writes to the
            # pipeline's worktree, not a slice PR, so no add/add conflict.
            write_per_slice=True,
            run_epoch=_run_epoch_str,
        )
    except Exception as brc_err:
        _pkg.logger.warning(
            "Failed to persist BRC history on cancel (continuing)",
            pipeline_id=pipeline.id,
            phase=current_phase,
            error=str(brc_err),
        )
        return

    try:
        _pkg._commit_statefiles_to_worktree(
            worktree_path,
            f"Persist BRC history on cancel ({current_phase} phase)",
            pipeline_identifier=_pkg._pipeline_identifier(pipeline.issue_number, pipeline.id),
            pipeline_id=pipeline.id,
        )
    except subprocess.CalledProcessError as git_err:
        _pkg.logger.warning(
            "Failed to commit BRC history on cancel (continuing)",
            pipeline_id=pipeline.id,
            phase=current_phase,
            error=str(git_err),
        )


def _commit_slice_brc_history_to_integration_branch(
    pipeline,
    spawner: "ContainerSpawner",  # noqa: UP037
    worktree_repo_path: Path,
    slice_id: str,
    integration_branch: str,
    *,
    gateway_mode: Literal["public", "private"] = "public",
) -> bool:
    """Commit a slice's per-slice BRC history onto its integration branch (#2548).

    Runs after the slice's implement-phase consensus is reached and
    before the slice PR is opened, so reviewers approaching the slice
    PR see the full BRC consensus transcript that approved the slice's
    code as part of the diff.

    Steps:

    1. Materialise a per-tick temp directory under
       ``WORKTREE_BASE_DIR`` (gateway-allowlisted; see #2684) and
       render the per-slice BRC history files into a ``staging/``
       subdirectory via :func:`_write_brc_history`. The writer pulls
       messages from the message store; the staging directory is
       scoped to this hook tick so concurrent slice hooks do not
       cross-write each other (#2755).
    2. Materialise a temporary **detached** git worktree on
       ``origin/<integration_branch>`` (the slice's integration branch).
       A detached worktree claims no branch ref, so it never collides
       with the slice's own agent worktrees — which hold the
       integration branch checked out for the duration of the slice
       run — nor with a prior tick that crashed mid-flight (#2778).
    3. Copy ONLY this slice's per-slice BRC files
       (``<identifier>-implement-<slice_id>.{json,md}``) from the
       staging directory to the integration worktree. Other slices'
       files (or the unattributed sibling) are deliberately not
       copied — each slice PR carries only its own BRC transcript per
       D2 / D5 of #2548.
    4. Commit via :func:`_commit_statefiles_to_worktree`
       (orchestrator-authored, ``--no-verify``, idempotent: skips when
       staged is empty).
    5. Push via :meth:`GatewayClient.push_worktree_branch` (launcher-
       auth so we bypass agent-facing push restrictions on
       ``.egg-state/brc-history/``).

    Returns ``True`` on success or no-op (files already committed and
    push is a fast-forward no-op).  Returns ``False`` on any failure;
    the caller treats this as best-effort and proceeds with PR
    creation. The per-slice BRC files do not exist on the work
    worktree under this design (#2755) — the integration branch is
    the only on-disk surface that carries them, so a failure here
    means the slice PR opens without its consensus transcript.

    Idempotency: every step is convergent — re-running mid-flight
    against an already-committed integration branch produces no new
    commit (``_commit_statefiles_to_worktree`` skips when nothing is
    staged) and a no-op fast-forward push.

    Concurrency: this hook runs from ``_run_one_slice_inner``, which
    is itself invoked concurrently across slices in a thread pool.
    Each invocation creates its own ``mkdtemp``-rooted staging
    directory, so two slices reaching consensus near-simultaneously
    do not share any filesystem state (#2755 fix). Each slice copies
    only its own per-slice files to its integration worktree
    (Step 3), so concurrent writes do not cross-pollinate slice PRs.
    """
    pipeline_id = pipeline.id

    if not pipeline.repo:
        _pkg.logger.info(
            "Per-slice BRC commit: pipeline has no remote repo, skipping (#2548)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
        )
        return False

    identifier = _pkg._brc_history_identifier(pipeline)

    import shutil
    import tempfile

    git_base = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        f"safe.directory={worktree_repo_path}",
        "-C",
        str(worktree_repo_path),
    ]

    # Root under WORKTREE_BASE_DIR so the temp path falls inside the
    # gateway's repo-path allowlist (gateway/git_client.py
    # ALLOWED_REPO_PATHS).  A ``/tmp`` location is rejected by
    # ``validate_repo_path``, which silently failed the BRC-history
    # push and left slice PRs without their consensus transcript
    # (#2684).  Falls back to system temp when the base dir is absent
    # (e.g. unit tests) — emit a warning on that branch so a broken
    # docker volume mount in production is noisy rather than silently
    # recreating the #2684 push-rejection.
    if _pkg.WORKTREE_BASE_DIR.exists():
        tmp_dir_base = str(_pkg.WORKTREE_BASE_DIR)
    else:
        _pkg.logger.warning(
            "Per-slice BRC commit: WORKTREE_BASE_DIR missing — falling "
            "back to system temp (likely a broken volume mount in "
            "production; the push to the integration branch will be "
            "rejected by the gateway allowlist) (#2684)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            integration_branch=integration_branch,
            worktree_base_dir=str(_pkg.WORKTREE_BASE_DIR),
        )
        tmp_dir_base = None
    tmp_worktree = Path(
        tempfile.mkdtemp(
            prefix=f"egg-slice-brc-{pipeline_id}-{slice_id}-",
            dir=tmp_dir_base,
        )
    )
    # Per-tick staging directory so concurrent slice hooks do not
    # share the writer's output (#2755). ``_write_brc_history``
    # renders into ``<staging>/.egg-state/brc-history/`` — same
    # relative layout it uses against a worktree — so the
    # ``Path.relative_to(staging)`` step below preserves the
    # canonical on-disk path when copying onto the integration
    # worktree.
    staging = tmp_worktree / "staging"
    wt_path = tmp_worktree / "wt"

    try:
        # --- Step 1: render the per-slice BRC files into the staging
        # directory.  The writer pulls messages from the message store
        # and writes all per-slice files for the implement phase; we
        # filter to this slice's files below.
        try:
            _pkg._write_brc_history(
                staging,
                pipeline_id,
                "implement",
                identifier,
            )
        except Exception as brc_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Per-slice BRC commit: failed to render BRC history into "
                "staging dir, skipping integration-branch commit (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(brc_err),
            )
            return False

        # The per-slice files we will copy onto the integration worktree.
        # Both files are produced by ``_write_brc_history`` (markdown and
        # JSON companion).  Missing files are tolerated — the writer logs
        # at warning level but still succeeds on the other format, so we
        # copy whichever exists.
        history_dir = staging / ".egg-state" / "brc-history"
        per_slice_files: list[Path] = []
        for ext in ("md", "json"):
            candidate = history_dir / f"{identifier}-implement-{slice_id}.{ext}"
            if candidate.is_symlink():
                # Defense-in-depth: a planted symlink could point outside
                # ``.egg-state/`` and leak unrelated content onto the slice
                # PR. The staging directory is freshly minted under
                # ``tempfile.mkdtemp`` per hook tick, so a symlink at this
                # path would have to come from the writer itself — the
                # check is cheap and protects against any future writer
                # change that might honour an attacker-controlled
                # metadata blob when synthesising the filename.
                _pkg.logger.warning(
                    "Per-slice BRC commit: skipping symlink in brc-history (#2548)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    path=str(candidate),
                )
                continue
            if candidate.is_file():
                per_slice_files.append(candidate)

        if not per_slice_files:
            _pkg.logger.warning(
                "Per-slice BRC commit: no per-slice BRC files produced "
                "for slice — skipping integration-branch commit (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                identifier=str(identifier),
            )
            return False

        # --- Step 2: refresh the local remote-tracking ref for the
        # integration branch.  The slice's agents pushed directly to
        # ``origin/<integration_branch>`` during the run, so the work
        # worktree's local tracking ref may lag.  Best-effort: a failure
        # here usually means the agent-side push has not yet propagated;
        # the worktree-add below would then fail and we'd return False.
        try:
            spawner.gateway.fetch_branch(
                pipeline_id,
                str(worktree_repo_path),
                args=[f"+refs/heads/{integration_branch}:refs/remotes/origin/{integration_branch}"],
                mode=gateway_mode,  # type: ignore[arg-type]
            )
        except Exception as fetch_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Per-slice BRC commit: fetch of integration branch failed (continuing) (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                integration_branch=integration_branch,
                error=str(fetch_err),
            )

        try:
            subprocess.run(
                [
                    *git_base,
                    "worktree",
                    "add",
                    # Detached, not ``-B <integration_branch>``: a branch
                    # can live in only one linked worktree, and the
                    # slice's agent worktrees already hold it — ``-B``
                    # lost that race with ``fatal: ... already used by
                    # worktree`` (#2778). See Step 2 in the docstring.
                    "--detach",
                    str(wt_path),
                    f"origin/{integration_branch}",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as wt_err:
            _pkg.logger.warning(
                "Per-slice BRC commit: worktree add failed, skipping (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                integration_branch=integration_branch,
                stderr=(wt_err.stderr or "")[:500],
            )
            return False

        # --- Step 3: copy ONLY this slice's BRC files onto the integration
        # worktree.  Each file lands at the same relative path it occupies
        # in the staging dir (``.egg-state/brc-history/...``).
        for src in per_slice_files:
            try:
                rel = src.relative_to(staging)
            except ValueError:
                _pkg.logger.warning(
                    "Per-slice BRC commit: file outside staging dir, skipping it (#2548)",
                    pipeline_id=pipeline_id,
                    slice_id=slice_id,
                    src=str(src),
                )
                continue
            dst = wt_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        # --- Step 4: commit (idempotent — skips when staged is empty) ---
        try:
            _pkg._commit_statefiles_to_worktree(
                wt_path,
                f"Persist BRC history for {slice_id} (#2548)",
                pipeline_identifier=identifier,
                pipeline_id=pipeline_id,
            )
        except Exception as commit_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Per-slice BRC commit: commit failed, skipping (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(commit_err),
            )
            return False

        # --- Step 5: push to origin/<integration_branch>.  Fast-forward
        # no-op when the local tip matches origin (e.g. when the
        # commit step was a no-op because everything was already
        # committed on a prior tick).
        try:
            push_result = spawner.gateway.push_worktree_branch(
                pipeline_id=pipeline_id,
                repo_path=str(wt_path),
                branch=integration_branch,
                mode=gateway_mode,  # type: ignore[arg-type]
                base_branch=pipeline.base_branch,
            )
        except Exception as push_err:  # noqa: BLE001
            _pkg.logger.warning(
                "Per-slice BRC commit: push raised, skipping (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(push_err),
            )
            return False
        if not push_result.ok:
            _pkg.logger.warning(
                "Per-slice BRC commit: push failed, skipping (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                category=getattr(push_result, "category", None),
                detail=getattr(push_result, "detail", None),
            )
            return False

        _pkg.logger.info(
            "Per-slice BRC commit: pushed BRC history to integration branch (#2548)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            integration_branch=integration_branch,
            files=[str(p.relative_to(staging)) for p in per_slice_files],
        )
        return True
    finally:
        # Best-effort cleanup of the temp worktree.  A failure here is a
        # housekeeping problem, not a pipeline-blocker.
        try:
            subprocess.run(
                [*git_base, "worktree", "remove", "--force", str(wt_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception as cleanup_err:  # noqa: BLE001
            _pkg.logger.debug(
                "Per-slice BRC commit: worktree remove failed (continuing) (#2548)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                error=str(cleanup_err),
            )
        try:
            shutil.rmtree(tmp_worktree, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
