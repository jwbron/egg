"""Per-producer delta builder — ``git log`` subprocess + delta entries.

:func:`_run_git_log` renders the per-producer re-review diff via a
``git log … -p`` subprocess; :func:`_build_delta_entries` scopes the
producer set to the current event and assembles one delta entry per
producer (with the ``changed_artifacts`` / served-read fallback when no
``last_reviewed_commit_sha`` is recorded). AST-identical to the
pre-split definitions — pure refactor (#3312 slice-6).

Patch-seam note: :func:`_build_delta_entries` calls ``_run_git_log``
through the package (barrel) module object, not the module-local name,
so ``patch("orchestrator.routes.event_prompt._run_git_log")`` (the
stable test seam) keeps intercepting the call after the decomposition.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from ._caps import _GIT_LOG_DELTA_MAX_BYTES, _GIT_LOG_TIMEOUT_SECS
from ._memory_io import _parse_per_producer_sha
from ._payload import (
    _extract_artifacts_for_producer,
    _extract_current_producers,
    _extract_proposal_sha_for_producer,
)


def _run_git_log(
    sha: str,
    base_branch: str,
    repo_path: Path,
    end_ref: str = "HEAD",
) -> str:
    """Render ``git log {sha}..{end_ref} --not origin/{base_branch} -p``.

    Runs the subprocess in ``repo_path``. The gateway allows
    ``git log`` with ``-p`` / ``--patch`` and ``--not`` flags (see
    ``gateway`` allow-list; #2905). On non-zero rc or timeout we
    return a sentinel string so the agent can audit the failure
    explicitly rather than silently reviewing an empty diff.

    ``end_ref`` defaults to ``HEAD`` for legacy payloads, but callers
    should pass the producer's ``proposal_commit_sha`` when the event
    payload carries one (#3076): the reviewer's own HEAD does not
    contain the producer's commits (per-role worktrees), so a
    ``{sha}..HEAD`` range in the reviewer's worktree is empty even
    when the producer revised — the "re-review delta is empty"
    phantom-NACK. The proposal SHA resolves from any agent worktree
    because all per-role worktrees share the host repo's object store.
    """
    cmd = [
        "git",
        "log",
        f"{sha}..{end_ref}",
        "--not",
        f"origin/{base_branch}",
        "-p",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"(git log timed out after {_GIT_LOG_TIMEOUT_SECS}s for {sha}..{end_ref})"
    except OSError as exc:  # pragma: no cover — defensive
        return f"(git log failed: {exc})"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return f"(git log returned rc={result.returncode}: {stderr or 'no stderr'})"

    payload = result.stdout or ""
    encoded = payload.encode("utf-8")
    if len(encoded) > _GIT_LOG_DELTA_MAX_BYTES:
        # Truncate at the byte cap and re-decode with replacement so a
        # UTF-8 multibyte sequence split at the boundary doesn't crash
        # the rendering. The truncation sentinel is on its own line so
        # the agent sees the cut explicitly.
        truncated = encoded[:_GIT_LOG_DELTA_MAX_BYTES].decode("utf-8", errors="replace")
        return (
            truncated
            + "\n…(truncated — delta exceeded "
            + f"{_GIT_LOG_DELTA_MAX_BYTES} bytes; the agent should pull "
            + "the full delta with the command above if a thorough audit is required)\n"
        )
    return payload


def _build_delta_entries(
    *,
    action: str,
    role: str,
    base_branch: str,
    repo_path: Path,
    memory_text: str,
    event_payload: Any = None,
) -> list[dict[str, Any]]:
    """Render per-producer deltas for the current action.

    For review actions (``ack`` / ``nack``) we render one delta per
    producer **scoped to the current event** (the producer(s) named
    in ``event_payload.pending_reviews``). For producer actions
    (``propose`` / ``confirm``) there is no per-producer delta — the
    producer just looks at HEAD.

    **Scoping invariant (reviewer_code_holistic v2 finding #3).** The
    memory file accumulates a per-producer ``last_reviewed_commit_sha``
    for every producer the reviewer has ever ACK/NACKed. The current
    event names *one* producer (or a small set in
    ``pending_reviews``) — the agent's job this invocation is to
    review THAT producer's latest delta, not to be handed stale
    deltas for unrelated prior producers. Treat the memory file as a
    per-producer LOOKUP keyed by the current event's producers, not
    as an ENUMERATION source. When the event payload doesn't name
    producers (legacy / synthetic / test paths) we fall back to
    rendering all stored SHAs — this preserves backward compatibility
    for callers that bypass the next-action route.

    **``changed_artifacts`` fallback (plan TASK-3-2 acceptance).**
    For each scoped producer with no stored SHA (first-ever ACK,
    parse failure, file missing) the renderer falls back to the
    orchestrator's signal-level artifact list. The fallback is sourced
    in priority order from (a) ``event_payload.pending_reviews[i].
    artifact_refs`` (the next-action route enriches this from
    ``PeerConsensusTracker.get_current_proposal_snapshot``), and (b)
    the top-level ``event_payload.changed_artifacts`` key (legacy /
    test path). The fallback is explicitly labelled as a degraded
    baseline so the agent does not mistake it for an
    adversarial-re-review-grade diff. The documenter's docs at
    ``docs/architecture/orchestrator.md`` and
    ``docs/reference/agent-wait-patterns.md`` describe the same
    fallback: "strictly a degraded baseline, not the adversarial
    re-review path".

    ``role`` is currently unused inside the function body; the
    parameter is retained for the call-site symmetry (architect
    plan: the composer signature passes role through alongside
    action). Once the next-action route is the only producer of
    event_payload, ``role`` can be re-purposed as the reviewer-role
    half of the (reviewer, producer) relationship; for now the
    producer derivation is event-payload-driven.
    """
    del role  # see docstring — kept for call-site symmetry
    if action not in ("ack", "nack"):
        return []

    per_producer_sha = _parse_per_producer_sha(memory_text)

    # Scope the producer set to the current event. Falls back to ALL
    # stored producers in memory when the event payload doesn't name
    # any (legacy callers, synthetic test payloads).
    scoped_producers = _extract_current_producers(event_payload)
    if not scoped_producers:
        scoped_producers = sorted(per_producer_sha.keys())

    out: list[dict[str, Any]] = []
    for producer in scoped_producers:
        sha = per_producer_sha.get(producer, "")
        # The producer's proposed commit SHA from pending_reviews
        # (#3076). When present it is BOTH the delta endpoint (the
        # reviewer's own HEAD never contains the producer's commits —
        # per-role worktrees — so ``{sha}..HEAD`` was empty even after
        # a revision) and the anchor for ``git show <sha>:<path>``
        # artifact reads, which resolve from any agent worktree via
        # the shared host object store.
        proposal_sha = _extract_proposal_sha_for_producer(event_payload, producer)
        if sha:
            # Route through the package (barrel) module object so the
            # stable test seam ``patch("…event_prompt._run_git_log")``
            # keeps intercepting this call post-decomposition.
            delta = sys.modules[__package__]._run_git_log(
                sha,
                base_branch,
                repo_path,
                end_ref=proposal_sha or "HEAD",
            )
            out.append(
                {
                    "producer": producer,
                    "last_reviewed_commit_sha": sha,
                    "proposal_commit_sha": proposal_sha,
                    "delta": delta,
                }
            )
            continue

        # Per-producer fallback — no recorded SHA for this producer.
        # Prefer per-producer artifact_refs from pending_reviews; fall
        # back to the legacy top-level changed_artifacts key.
        artifacts = _extract_artifacts_for_producer(event_payload, producer)
        if not artifacts and not proposal_sha:
            continue
        # Strip backticks from artifact paths before interpolation. The
        # paths are producer-supplied through ``snapshot["artifacts"]``;
        # ``proposal_sha`` is hex-validated upstream, but ``a`` is not.
        # The agent (not bash) is the consumer here so this is not a
        # shell-injection vector, but a stray backtick in a path would
        # break the markdown code span the agent renders. Defensive
        # belt-and-braces rather than trusting producer payloads.
        artifacts = [a.replace("`", "") for a in artifacts]
        refs_text = "\n".join(f"- `{a}`" for a in artifacts)
        if proposal_sha:
            # First review with a known proposal SHA. The producer's work
            # is NOT in this reviewer's worktree — per-role worktrees are
            # isolated, and #3216 (WS1 of #3209) stops syncing peer trees
            # into read-only reviewers — so render served reads keyed by
            # artifact NAME rather than path-bearing `git show <sha>:<path>`
            # commands. `egg-artifact` resolves the repo path server-side
            # from the spec registry and streams the committed bytes at
            # <sha>, regardless of whether the commit resolves in this
            # worktree's object store (the #3002 split-store case the old
            # `git show` channel breaks on). Registered coordination
            # artifacts get a per-name read; anything unregistered is
            # covered by the full proposed-change delta below.
            from egg_contracts.artifact_spec import name_for_path

            read_names: list[str] = []
            for a in artifacts:
                name = name_for_path(a)
                if name and name not in read_names:
                    read_names.append(name)
            read_cmds = "\n".join(
                f"- `egg-artifact get {name} --ref {proposal_sha}`" for name in read_names
            )
            fallback_delta = (
                "(No `last_reviewed_commit_sha` recorded yet for this "
                "producer — this is your FIRST review of this proposal. "
                "The producer's work is NOT in your working tree; per-role "
                "worktrees are isolated. Read each registered coordination "
                f"artifact at the proposed commit `{proposal_sha}` via the "
                "served read — it resolves the artifact server-side from "
                "its spec-registered name, no local checkout required:)\n\n"
                + (f"Proposed artifacts:\n{read_cmds}\n\n" if read_cmds else "")
                + "Full proposed change:\n"
                f"- `git log {proposal_sha} --not origin/{base_branch} -p`\n\n"
                "Do NOT NACK for a missing file before reading it via "
                "these commands — a plain `Read` of the path in your own "
                "worktree is expected to fail and is not evidence the "
                "artifact does not exist.\n"
            )
        else:
            # No proposal SHA in the payload AND no recorded
            # ``last_reviewed_commit_sha`` for this producer — degraded
            # baseline. #3077 slice-5 task-5-1 deletes the prior
            # "fetch and read the actual file diffs yourself" prose:
            # per-role worktrees mean the agent CANNOT recover the
            # producer's work via its own ``git fetch`` (the producer's
            # commits live in the host object store, which the wrapper
            # syncs into the agent worktree before this prompt is
            # rendered when a proposal SHA is known). With no SHA there
            # is nothing to render a ``git show`` against, so we surface
            # the orchestrator's signal-level artifact list and stop —
            # the agent must NOT self-fetch.
            fallback_delta = (
                "(No `last_reviewed_commit_sha` recorded yet for this "
                "producer and no proposal SHA in the event payload — "
                "falling back to the orchestrator's signal-level "
                "`changed_artifacts` list as a degraded baseline. This is "
                "NOT the adversarial-re-review path.)\n\n"
                f"Artifacts the orchestrator flagged as changed:\n{refs_text}\n"
            )
        out.append(
            {
                "producer": producer,
                "last_reviewed_commit_sha": "",
                "proposal_commit_sha": proposal_sha,
                "delta": fallback_delta,
            }
        )
    return out
