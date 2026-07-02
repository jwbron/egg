"""Pull-request create / update / list / lookup + repo visibility (#3312).

Private submodule of the ``gateway_client`` sub-package; import through the
barrel (``from gateway_client import ...``), not directly.
"""

import uuid
from typing import Any, Literal
from urllib.parse import quote

import gateway_client as _pkg
from gateway_client._pr_format import (
    _append_diff_summary_section,
    _append_this_slice_section,
    _derive_program_slug,
    _first_sentence,
    _format_position_marker,
    _format_slice_title,
    _format_stack_block,
    _truncate_title,
)

try:
    from egg_contracts.markdown import unwrap_soft_breaks
except ImportError:  # pragma: no cover

    def unwrap_soft_breaks(text: str | None) -> str:  # type: ignore[misc]
        return text or ""


def create_pr(
    self,
    pipeline_id: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str | None = None,
    issue_number: int | None = None,
    agent_role: str | None = None,
    mode: Literal["public", "private"] = "public",
    draft: bool = False,
) -> str | None:
    """Create a pull request via the gateway using a temporary session.

    Registers a synthetic temp session WITHOUT a phase value (#2777
    TASK-2-2): the gateway's gh_pr_create handler treats a
    ``session_phase`` of ``None`` as the explicit-opt-out path and
    skips phase-filter consultation entirely ("No phase set - allow
    by default for backward compatibility" branch at
    ``gateway/gateway.py:3685``). Prior to #2777 the carve-out used
    ``phase="pr"`` paired with the now-removed ``PipelinePhase.PR``
    enum row; that coupling was deleted lock-step so the orchestrator
    no longer has any pipeline-graph reference to a PR phase.
    The synthetic-session trust gate (``synthetic=True`` is only
    settable by the launcher-authenticated ``register_session``
    path) is unchanged and remains the load-bearing protection
    against a sandboxed agent reaching this surface.

    Args:
        pipeline_id: Pipeline ID (used as container_id for the temp session)
        repo: Repository in owner/name format
        title: PR title
        body: PR body/description
        head: Head branch name
        base: Base branch name (default: None, gateway auto-detects)
        issue_number: Optional issue number for pipeline metadata
        agent_role: Optional agent role for pipeline metadata

    Returns:
        PR URL if creation succeeded, None otherwise

    Raises:
        GatewayError: On request failure. Unlike push_worktree_branch/
            delete_remote_branch (which catch errors internally and
            return PushResult — truthy on success) or
            fetch_worktree_branch (which returns bool), this method
            lets errors propagate so the caller can decide whether a
            failed PR creation should abort the phase.
    """
    temp_container_id = f"{pipeline_id}-auto-pr"
    session_token: str | None = None
    try:
        session = self.register_session(
            container_id=temp_container_id,
            container_ip=self.self_ip,
            mode=mode,
            pipeline_id=pipeline_id,
            # phase=None (#2777 TASK-2-2): the synthetic-session
            # carve-out for gh_pr_create no longer goes through
            # PipelinePhase.PR — the gateway treats a phase-less
            # synthetic session as explicit opt-out. See docstring.
            repos=[repo],
            issue_number=issue_number,
            agent_role=agent_role,
            synthetic=True,
        )
        session_token = session.session_token

        pr_data: dict[str, Any] = {
            "repo": repo,
            "title": title,
            "body": body,
            "head": head,
            "draft": draft,
        }
        if base:
            pr_data["base"] = base

        result = self._make_request(
            "/api/v1/gh/pr/create",
            method="POST",
            data=pr_data,
            bearer_token=session_token,
        )

        pr_url: str | None = None
        stdout = result.get("data", {}).get("stdout", "")
        if stdout:
            # gh pr create outputs the PR URL on stdout
            pr_url = stdout.strip()

        _pkg.logger.info(
            "Auto-created PR via gateway",
            pipeline_id=pipeline_id,
            repo=repo,
            head=head,
            pr_url=pr_url,
        )
        return pr_url
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def _format_pr_ref(ref: dict[str, Any]) -> str | None:
    """Render one cross-repo PR reference as ``owner/repo#N`` (#3393).

    GitHub autolinks the ``owner/repo#N`` form to the PR in that repo
    (a bare ``#N`` would resolve against the repo the body lives in,
    which is the wrong repo for a cross-repo reference). Returns
    ``None`` when the ref lacks a repo or a positive integer number so
    the caller can skip malformed entries rather than emit a dead link.
    """
    repo = (ref.get("repo") or "").strip()
    number = ref.get("number")
    if not repo or not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    return f"{repo}#{number}"


def _append_related_prs_section(
    body_lines: list[str],
    sibling_pr_refs: list[dict[str, Any]] | None,
    upstream_pr_ref: dict[str, Any] | None,
) -> None:
    """Append the cross-repo ``## Related PRs`` section (#3393, slice-4).

    ``sibling_pr_refs`` are the pipeline's PRs in OTHER repos; the
    ``upstream_pr_ref`` is the cross-repo upstream slice's PR this
    slice is ordered behind. Malformed entries (no repo / no valid
    number) are dropped. When nothing renders (the N=1 case — all
    relationships are same-repo and covered by ``## Stack``) the
    section is omitted entirely so the body is unchanged.
    """
    sibling_lines: list[str] = []
    seen: set[str] = set()
    for ref in sibling_pr_refs or []:
        rendered = _format_pr_ref(ref)
        if rendered and rendered not in seen:
            seen.add(rendered)
            sibling_lines.append(f"- {rendered}")

    upstream_rendered = _format_pr_ref(upstream_pr_ref) if upstream_pr_ref else None

    if not sibling_lines and not upstream_rendered:
        return

    body_lines.append("## Related PRs")
    body_lines.append("")
    if upstream_rendered:
        body_lines.append(f"Ordered behind upstream PR: {upstream_rendered}")
        body_lines.append("")
    if sibling_lines:
        body_lines.append("Coordinated PRs in other repos in this pipeline:")
        body_lines.append("")
        body_lines.extend(sibling_lines)
        body_lines.append("")


def create_slice_pr(
    self,
    pipeline_id: str,
    repo: str,
    *,
    slice_id: str,
    slice_name: str,
    slice_tasks: list[dict[str, Any]] | None,
    head: str,
    base: str,
    issue_number: int | None = None,
    agent_role: str | None = None,
    mode: Literal["public", "private"] = "public",
    draft: bool = False,
    program_title: str | None = None,
    program_description: str | None = None,
    program_test_plan: str | None = None,
    program_manual_steps: str | None = None,
    slice_index: int | None = None,
    slice_count: int | None = None,
    slice_files_affected: list[str] | None = None,
    context_pr_number: int | None = None,
    slice_goal: str | None = None,
    diffstat: str | None = None,
    commit_subjects: list[str] | None = None,
    sibling_pr_refs: list[dict[str, Any]] | None = None,
    upstream_pr_ref: dict[str, Any] | None = None,
) -> str | None:
    """Open a PR for one slice in a stacked-PR chain.

    Slice PRs are scoped to their own slice: subject + files
    affected + full task descriptions with acceptance criteria.
    Strategic context (analysis doc, plan doc, refine/plan BRC
    history) AND execution-time concerns (test plan, manual steps,
    pre-merge obligations) all live on the up-front context PR
    opened by :func:`_open_context_pr_at_implement_start`
    (``egg/<id>/work → main``, #2777 cq-4). Slice PRs link to it
    via ``context_pr_number`` and otherwise stay focused on the
    slice's own diff.

    Pre-#2777 cq-6 the terminal slice carried a program-level
    rollup body (test plan / manual steps / pre-merge obligations
    + a ``[merge-gate]`` title marker). That treatment is gone —
    the merge gate is now the up-front context PR, not the last
    slice in the stack — so every slice PR uses the same uniform
    shape based only on whether the context PR exists.

    * **Title.** ``[<program-slug>][slice-N/M] <slice subject>``.
      ``<program-slug>`` is derived from ``pipeline_id``
      (``issue-<N>`` collapsed to ``issue-<N>``; ``pipeline-<hash>``
      truncated). When ``program_title`` is empty (older contracts
      / planner skipped the field), titles fall back to the
      deterministic ``{slice_id}: {slice_name}`` form (#2539).
      Over-long titles truncate at a word boundary (#3115).
    * **Body (uniform shape, #3115).** Lead paragraph (the
      planner's reviewer-facing ``slice_goal``; falls back to the
      first sentence of ``program_description``) →
      ``**Base PR:** #<context_pr_number>`` whenever the number is
      known → ``## What's in this PR`` (commit subjects + diffstat
      computed from the pushed branch, when the caller supplies
      them) → ``## This slice`` (subject, files affected, full
      task descriptions + acceptance criteria behind a
      ``<details>`` fold) → ``## Stack`` (base PR, position).
    * **No context PR — UX backstop.** When ``context_pr_number``
      is missing, the program narrative (description + test plan +
      manual steps) is inlined around the sections above so the
      slice PR is still reviewable as a standalone diff against
      ``/work``. NOTE: under cq-4 the context PR is hard-required
      and this branch should be unreachable in production; it
      stays as defence-in-depth so the slice PR body still renders
      something useful if the new opener somehow failed to persist
      ``context_pr_number``. The stack is structurally unmergeable
      in that state — fixing the body here is not a fix for the
      missing-context-PR structural break.

    Idempotency (#2777 cq-8 / task-3-2). Before invoking
    ``gh pr create``, an existing open PR with the same head +
    base is looked up via :meth:`lookup_open_pr`; on hit the
    existing PR URL is returned and ``gh pr create`` is skipped.
    This prevents a transient ``gh pr create`` failure that
    partially succeeded (PR created, network blip on response)
    from cascading the slice to FAILED on retry.

    Cross-repo coordination (#3393, slice-4 / task-4-1). When the
    pipeline spans more than one repo, ``sibling_pr_refs`` carries
    the pipeline's PRs that live in a DIFFERENT repo than this
    slice, and ``upstream_pr_ref`` names the cross-repo upstream
    slice's PR this slice is ordered behind. Both render a
    ``## Related PRs`` section so a reviewer opening one repo's PR
    can navigate the coordinated change across repos. Both are
    scoped to CROSS-repo relationships only (same-repo slices are
    already linked via the ``## Stack`` block), so for an N=1
    single-repo pipeline both are empty/None, the section is
    omitted, and the body is byte-identical to the pre-#3393 shape.
    """
    has_program_title = bool(program_title and program_title.strip())
    has_base_pr = context_pr_number is not None and context_pr_number >= 1

    program_slug = _derive_program_slug(pipeline_id)
    position_marker = _format_position_marker(slice_id, slice_index, slice_count)

    if has_program_title:
        assert program_title is not None  # implied by has_program_title
        subject = (slice_name or slice_id).strip() or slice_id
        title = _format_slice_title(program_slug, position_marker, subject)
    else:
        title = f"{slice_id}: {slice_name}".strip()
    title = _truncate_title(title)

    body_lines: list[str] = []

    # Lead paragraph (#3115): the planner's reviewer-facing slice
    # ``goal``. When absent (pre-#3115 contracts), fall back to the
    # first sentence of the program description — except on the
    # no-base-PR backstop branch, which inlines the full program
    # description just below (the blurb would duplicate its first
    # sentence).
    inline_program_narrative = has_program_title and not has_base_pr
    # Soft-break unwrapping (#3122): the goal / description reach us
    # as YAML block scalars hard-wrapped at ~75 chars, and GitHub
    # renders every newline in a PR body as a line break.
    lead = unwrap_soft_breaks(slice_goal).strip()
    if not lead and program_description and not inline_program_narrative:
        lead = _first_sentence(unwrap_soft_breaks(program_description))
    if lead:
        body_lines.append(lead)
        body_lines.append("")

    if has_base_pr:
        # Rendered on every branch that knows the number (#3115) —
        # previously the no-program-title fallback dropped the link
        # even when the context PR existed.
        body_lines.append(f"**Base PR:** #{context_pr_number}")
        body_lines.append("")
    elif inline_program_narrative:
        # UX backstop: ``context_pr_number`` is missing (under cq-4
        # this should be unreachable since the new opener is
        # hard-required). Inline the program narrative so the slice
        # PR remains reviewable as a standalone diff against
        # ``/work``. The stack is structurally unmergeable in this
        # state — fixing the body here is a UX backstop, not a fix.
        if program_description and program_description.strip():
            body_lines.append(unwrap_soft_breaks(program_description).strip())
            body_lines.append("")

    _append_diff_summary_section(body_lines, diffstat, commit_subjects)
    _append_this_slice_section(body_lines, slice_name, slice_files_affected, slice_tasks)

    if inline_program_narrative:
        if program_test_plan and program_test_plan.strip():
            body_lines.append("## Test Plan")
            body_lines.append("")
            body_lines.append(unwrap_soft_breaks(program_test_plan).strip())
            body_lines.append("")
        if program_manual_steps and program_manual_steps.strip():
            body_lines.append("## Manual Steps")
            body_lines.append("")
            body_lines.append(unwrap_soft_breaks(program_manual_steps).strip())
            body_lines.append("")

    # ``## Related PRs`` — cross-repo coordination section (#3393,
    # slice-4). Rendered only when this pipeline coordinates PRs in
    # another repo: sibling PRs living in a different repo than this
    # slice, plus the cross-repo upstream PR this slice is ordered
    # behind. Empty for an N=1 pipeline (all relationships are
    # same-repo and already covered by ``## Stack``), so the body
    # stays byte-identical there.
    _append_related_prs_section(body_lines, sibling_pr_refs, upstream_pr_ref)

    # ``## Stack`` block — parent PR + base PR + position. Replaces
    # the old "Slice X of pipeline Y. Stacked on top of `<base>`."
    # footer with structured links so reviewers can navigate the
    # stack without leaving the PR.
    stack_lines = _format_stack_block(
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        slice_index=slice_index,
        slice_count=slice_count,
        base_branch=base,
        context_pr_number=context_pr_number,
    )
    body_lines.extend(stack_lines)
    body = "\n".join(body_lines)

    # Idempotency pre-flight (#2777 cq-8 / task-3-2): if an open PR
    # already exists for this head + base, return its URL instead of
    # invoking ``gh pr create``. Prevents a partial-success retry
    # (PR created server-side, transport blip on the response) from
    # cascading the slice to FAILED on the next tick.
    if repo:
        # Idempotency lookup runs on the control-plane route
        # (``/api/v1/gh/find_open_pr``, launcher auth) — the
        # orchestrator is the control plane, not an agent, so the
        # caller's ``agent_role`` is irrelevant here (#2893 follow-up).
        existing_pr_number = self.lookup_open_pr(
            pipeline_id=pipeline_id,
            repo=repo,
            head=head,
            base=base,
        )
        if existing_pr_number is not None:
            existing_url = f"https://github.com/{repo}/pull/{existing_pr_number}"
            _pkg.logger.info(
                "Slice PR already exists; returning existing PR (idempotent path)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                head=head,
                base=base,
                pr_number=existing_pr_number,
                pr_url=existing_url,
            )
            return existing_url

    return self.create_pr(
        pipeline_id=pipeline_id,
        repo=repo,
        title=title,
        body=body,
        head=head,
        base=base,
        issue_number=issue_number,
        agent_role=agent_role,
        mode=mode,
        draft=draft,
    )


def update_pr_body(
    self,
    pipeline_id: str,
    repo: str,
    *,
    pr_number: int,
    body: str,
    issue_number: int | None = None,
    agent_role: str | None = None,
    mode: Literal["public", "private"] = "public",
) -> bool:
    """Replace an existing PR's body via the gateway (#3122).

    Routes through the per-agent ``/api/v1/gh/pr/edit`` endpoint
    (``gh api repos/<repo>/pulls/<n> -X PATCH -f body=...``) under a
    synthetic phase-less session, exactly like :meth:`create_pr` /
    :meth:`rebase_onto` — no new privileged orchestrator endpoint.
    The gateway's PR-ownership policy still applies, which is the
    desired bound: the orchestrator only rewrites PRs the egg bot
    user authored.

    Sole production caller is the run loop's context-PR refresh:
    after a slice PR opens, the context PR body is recomposed with a
    link to it. Pipeline-generated PR bodies are machine-owned —
    each call fully replaces the body, clobbering manual edits.

    Returns ``True`` on success, ``False`` on any failure. Unlike
    :meth:`create_pr` this method does NOT propagate errors: a body
    refresh is cosmetic, and no caller should fail a slice over it.
    """
    if (
        not repo
        or pr_number is None
        or isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number < 1
    ):
        _pkg.logger.warning(
            "update_pr_body: invalid repo/pr_number",
            pipeline_id=pipeline_id,
            repo=repo,
            pr_number=pr_number,
        )
        return False

    # The gateway's ``gh pr edit`` rejects an empty payload (#3431
    # in gateway/gateway.py) — short-circuit before burning a
    # synthetic-session create+delete round-trip on a guaranteed
    # 400.
    if not body:
        _pkg.logger.warning(
            "update_pr_body: empty body",
            pipeline_id=pipeline_id,
            repo=repo,
            pr_number=pr_number,
        )
        return False

    # Suffix the container id with a short random tag so two
    # concurrent refreshes for the same pipeline (two slices in
    # the same wave finishing within ms of each other) don't share
    # a session-table key in the gateway. Matches the
    # per-slice-id uniqueness create_pr / rebase_onto already get
    # for free.
    temp_container_id = f"{pipeline_id}-pr-body-update-{uuid.uuid4().hex[:8]}"
    session_token: str | None = None
    try:
        session = self.register_session(
            container_id=temp_container_id,
            container_ip=self.self_ip,
            mode=mode,
            pipeline_id=pipeline_id,
            repos=[repo],
            issue_number=issue_number,
            agent_role=agent_role,
            synthetic=True,
        )
        session_token = session.session_token

        self._make_request(
            "/api/v1/gh/pr/edit",
            method="POST",
            data={
                "repo": repo,
                "pr_number": int(pr_number),
                "body": body,
            },
            bearer_token=session_token,
        )
        _pkg.logger.info(
            "Updated PR body via gateway",
            pipeline_id=pipeline_id,
            repo=repo,
            pr_number=pr_number,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # Session registration + single gh-pr-edit HTTP call.
        # Catches GatewayError and OSError (DNS / socket).
        _pkg.logger.warning(
            "update_pr_body: gateway request failed",
            pipeline_id=pipeline_id,
            repo=repo,
            pr_number=pr_number,
            error=str(exc),
        )
        return False
    finally:
        if session_token:
            try:
                self.delete_session(session_token)
            except Exception:
                pass


def list_open_prs(
    self,
    pipeline_id: str,
    repo: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List open PRs in ``repo`` via the orchestrator-only control-plane route.

    Returns a list of PR dicts with ``number``, ``head_ref``,
    ``base_ref`` shaped to match
    :func:`stacked_pr_reconciler.find_orphaned_child_prs`'s contract.

    Calls ``/api/v1/gh/list_open_prs`` with launcher auth (the control
    plane holds the launcher secret), not a synthetic agent session.
    The gateway runs ``gh pr list --repo <repo> --state open --limit
    <N> --json number,headRefName,baseRefName`` server-side. This is
    the seam #2922 established for :meth:`lookup_open_pr`; #2925
    completes the migration so the orchestrator is never modelled as an
    ``AgentRole`` — it authenticates as the control plane, not as an
    agent.

    On any error (gateway 4xx/5xx, JSON parse failure) the function
    logs and returns an empty list — the context-PR opener and the
    stacked-PR reconciler treat this as "no existing PR / see no
    orphans this tick", which is safe (the opener falls through to
    ``gh pr create``).
    """
    if not repo:
        return []
    try:
        result = self._make_request(
            "/api/v1/gh/list_open_prs",
            method="POST",
            data={"repo": repo, "limit": int(limit)},
            use_launcher_auth=True,
        )
        items = (result.get("data", {}) or {}).get("prs", []) or []
        if not isinstance(items, list):
            return []

        normalised: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            head_ref = item.get("headRefName") or item.get("head_ref") or ""
            base_ref = item.get("baseRefName") or item.get("base_ref") or ""
            if number is None or not head_ref:
                continue
            try:
                number_int = int(number)
            except TypeError, ValueError:
                continue
            normalised.append(
                {
                    "number": number_int,
                    "head_ref": str(head_ref),
                    "base_ref": str(base_ref),
                }
            )
        return normalised
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "list_open_prs: gateway request failed",
            pipeline_id=pipeline_id,
            repo=repo,
            error=str(exc),
        )
        return []


def lookup_open_pr(
    self,
    pipeline_id: str,
    repo: str,
    *,
    head: str,
    base: str,
) -> int | None:
    """Server-side idempotency check: return the open ``head → base`` PR number, or None.

    Calls the orchestrator-only control-plane route
    ``/api/v1/gh/find_open_pr`` with launcher auth. The gateway runs
    ``gh pr list --head <head> --base <base> --state open --json
    number`` server-side and returns the single matching PR number.

    Used by :meth:`create_slice_pr` to skip ``gh pr create`` when a
    slice PR with the same head + base is already open (#2777 cq-8
    / task-3-2 idempotency pre-flight).

    The orchestrator authenticates here as the **control plane** (the
    launcher secret), not as an agent. This is the seam #2893 should
    have used: the orchestrator is the server that manages pipelines,
    not an ``AgentRole``, so it does not register a synthetic agent
    session or impersonate a role on the per-agent ``/api/v1/gh/execute``
    surface.

    Returns:
        The integer PR number on hit, ``None`` on miss OR on any
        transport / parse error. The caller falls through to
        ``gh pr create`` either way — a transient lookup failure
        must not block PR creation.
    """
    if not repo:
        return None
    if not head or not base:
        # Defensive: never invoke ``gh pr list`` with an empty
        # filter (would return every open PR in the repo and a
        # caller-side ``if existing is not None`` would match the
        # first one, spuriously treating an unrelated PR as the
        # slice PR's idempotent hit).
        return None
    try:
        result = self._make_request(
            "/api/v1/gh/find_open_pr",
            method="POST",
            data={"repo": repo, "head": head, "base": base},
            use_launcher_auth=True,
        )
        number = (result.get("data", {}) or {}).get("number")
        if number is None:
            return None
        try:
            return int(number)
        except TypeError, ValueError:
            return None
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "lookup_open_pr: gateway request failed (treating as miss)",
            pipeline_id=pipeline_id,
            repo=repo,
            head=head,
            base=base,
            error=str(exc),
        )
        return None


def get_pr_merge_state(
    self,
    pipeline_id: str,
    repo: str,
    *,
    pr_number: int,
) -> dict[str, Any] | None:
    """Read an upstream PR's merge state via the control-plane route (#3393).

    Calls the orchestrator-only ``/api/v1/gh/pr/merge_state`` endpoint
    (launcher auth) which runs ``gh pr view <n> --repo <repo> --json
    state,mergedAt`` server-side. Returns a normalised dict::

        {"state": "OPEN"|"CLOSED"|"MERGED"|None, "merged_at": str|None}

    The cross-repo merge-sequencing gate keys its draft→ready decision
    off ``merged_at`` / ``state`` (NOT head-SHA equality — a squash or
    rebase merge yields a merge-commit SHA that differs from the PR
    head, so a SHA comparison would misfire; #3393 task-5-1 pin (a)).

    Returns ``None`` on any transport/parse error OR invalid input,
    which the gate treats as "state unknown this tick" (a pending, not
    terminal, outcome — the bounded poll retries and eventually
    escalates to a HITL hold if the upstream never resolves).
    """
    if not repo or isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return None
    try:
        result = self._make_request(
            "/api/v1/gh/pr/merge_state",
            method="POST",
            data={"repo": repo, "pr_number": int(pr_number)},
            use_launcher_auth=True,
        )
        payload = (result.get("data", {}) or {}) if isinstance(result, dict) else {}
        if not isinstance(payload, dict):
            return None
        return {
            "state": payload.get("state"),
            "merged_at": payload.get("mergedAt") or payload.get("merged_at"),
        }
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "get_pr_merge_state: gateway request failed (treating as unknown)",
            pipeline_id=pipeline_id,
            repo=repo,
            pr_number=pr_number,
            error=str(exc),
        )
        return None


def mark_pr_ready(
    self,
    pipeline_id: str,
    repo: str,
    *,
    pr_number: int,
) -> bool:
    """Transition a draft PR to ready via the control-plane route (#3393).

    Calls the orchestrator-only ``/api/v1/gh/pr/ready`` endpoint
    (launcher auth) which wraps ``gh pr ready <n> --repo <repo>``. Used
    by the cross-repo merge-sequencing gate to auto-ready a downstream
    dependent PR once its upstream slice PR merges.

    Returns ``True`` on success, ``False`` on any failure. The gate
    tracks per-slice progress and does not re-ready within a run, so a
    ``False`` return is logged and simply retried on the next reconcile
    tick — no caller aborts on it.
    """
    if not repo or isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number < 1:
        return False
    try:
        self._make_request(
            "/api/v1/gh/pr/ready",
            method="POST",
            data={"repo": repo, "pr_number": int(pr_number)},
            use_launcher_auth=True,
        )
        _pkg.logger.info(
            "Marked cross-repo dependent PR ready via gateway (#3393)",
            pipeline_id=pipeline_id,
            repo=repo,
            pr_number=pr_number,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _pkg.logger.warning(
            "mark_pr_ready: gateway request failed",
            pipeline_id=pipeline_id,
            repo=repo,
            pr_number=pr_number,
            error=str(exc),
        )
        return False


def get_repo_visibility(self, repo: str) -> str | None:
    """Query repo visibility from gateway.

    Args:
        repo: Repository in owner/name format

    Returns:
        Visibility string ('public', 'private', 'internal') or None on failure
    """
    try:
        result = self._make_request(
            f"/api/v1/repos/visibility?repos={quote(repo, safe='')}",
            use_launcher_auth=True,
        )
        visibilities = result.get("data", {}).get("visibilities", {})
        return visibilities.get(repo)
    except Exception as e:
        _pkg.logger.warning("Failed to query repo visibility", repo=repo, error=str(e))
        return None
