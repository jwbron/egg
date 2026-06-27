"""Plan / slice-DAG validators for the plan parser.

Holds the post-parse structural validators: forest / cycle checks
(#2137), slice file-overlap ordering (#3046), task role<->files alignment
(#2527), and the plan-phase pre-flight gate (#2777). Extracted verbatim
from the pre-split ``plan_parser.py`` (#3312 slice-7); every function is
AST-identical and re-exports through the package barrel.

``validate_plan_preflight`` reaches ``parse_plan`` through the package
module object (``import egg_contracts.plan_parser as _pkg``) so the
pre-split module-global ``patch("egg_contracts.plan_parser.parse_plan")``
seam keeps intercepting it -- preserving the exact patch behaviour the
single-file module had.
"""

from __future__ import annotations

import posixpath
from typing import Any

from egg_restrictions.matchers import match_pattern

import egg_contracts.plan_parser as _pkg

from ..models import Slice, Task
from ._models import PlanPreflightError


def validate_forest(slices: list[Slice]) -> list[str]:
    """Walk the slice DAG and reject any slice with >1 parent.

    Added in #2137 (TASK-2-2). The slice scheduler / stacked-PR
    machinery requires the implement-phase slice DAG to be a forest:
    each slice has at most one DAG parent. Multi-parent slices break
    the stacking invariant (a child PR has exactly one base) and are
    rejected at plan ingestion so the plan reviewer NACKs the planner.

    Args:
        slices: The slice list extracted from the contract / plan.

    Returns:
        A list of structured-error strings — one entry per offending
        slice. An empty list means the DAG is a valid forest. Each
        entry is a human-readable, reviewer-NACK-able message that
        explicitly names the offender, its parents, and the
        ``serialized_chain_order`` remediation (per refine-phase
        decision-17).
    """
    errors: list[str] = []
    seen_ids: set[str] = set()
    for slice_ in slices:
        if slice_.id in seen_ids:
            errors.append(
                f"Duplicate slice id '{slice_.id}' — every slice must have a "
                "unique identifier within the contract"
            )
        seen_ids.add(slice_.id)

    for slice_ in slices:
        deps = slice_.dependencies or []
        # Filter out unknown dependency targets — those are a separate
        # ingestion error and would otherwise drown the forest signal.
        real_parents = [d for d in deps if d in seen_ids]
        if len(real_parents) > 1:
            errors.append(
                f"Slice '{slice_.id}' has {len(real_parents)} DAG parents "
                f"({sorted(real_parents)!r}); the implement-phase slice DAG "
                "must be a forest (≤1 parent per slice). Serialise the "
                "upstream cluster into a chain and record the chosen order "
                "on this slice's 'serialized_chain_order' field — see "
                "issue #2137 plan TASK-2-3 for the auto-serialization rule."
            )

    # Cycle detection — a forest is by definition acyclic. A cyclic
    # ``slice-1 → slice-2 → slice-1`` chain has every slice with
    # exactly one parent, so the parent-count check above lets it
    # through; without this DFS the run loop's
    # ``while not scheduler.all_done():`` would spin forever.
    cycle_offenders = _detect_cycles(slices, seen_ids)
    for cycle in cycle_offenders:
        errors.append(
            f"Slice DAG contains a cycle: {' → '.join(cycle + [cycle[0]])}. "
            "Slices form an acyclic forest — break the cycle by removing "
            "or re-pointing one of the offending dependencies."
        )

    return errors


def _detect_cycles(slices: list[Slice], known_ids: set[str]) -> list[list[str]]:
    """Return a list of one slice-id chain per cycle in the slice DAG.

    DFS-based cycle detection. Returns one representative chain per
    cycle (so a 3-node cycle reports once, not three times). Unknown
    ids in ``dependencies`` are silently skipped here — they're
    reported by other validators.
    """
    adj: dict[str, list[str]] = {}
    for slice_ in slices:
        deps = [d for d in (slice_.dependencies or []) if d in known_ids]
        adj[slice_.id] = deps

    visited: set[str] = set()
    on_stack: set[str] = set()
    cycles: list[list[str]] = []
    seen_cycles: set[frozenset[str]] = set()

    def dfs(node: str, path: list[str]) -> None:
        visited.add(node)
        on_stack.add(node)
        path.append(node)
        for nxt in adj.get(node, []):
            if nxt in on_stack:
                # Found a cycle — slice the path from where ``nxt``
                # was first seen to ``node`` inclusive.
                if nxt in path:
                    cycle = path[path.index(nxt) :]
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(list(cycle))
            elif nxt not in visited:
                dfs(nxt, path)
        on_stack.discard(node)
        path.pop()

    for node in adj:
        if node not in visited:
            dfs(node, [])

    return cycles


def validate_slice_file_overlap(slices: list[Slice]) -> list[str]:
    """Reject slice pairs that share files but lack a dependency ordering.

    Added in #3046. The implement phase cuts each slice's integration
    branch off its dependency parent (root slices off the pipeline
    ``work`` branch) and ships it as a stacked PR. Two slices whose
    ``files_affected`` sets intersect must therefore be **ordered** along
    the dependency DAG — one a transitive ancestor of the other — so the
    later slice's branch is forked from a base that already contains the
    earlier slice's commits. When two overlapping slices are left
    unordered (e.g. both declared as roots), their branches fork
    independently off the shared base and their edits to the shared file
    collide at integration time. This is the guaranteed modify/delete
    conflict observed on #3023, where three slices all touched
    ``consensus_wrapper.py`` (one deleting it) with no dependency edges —
    the git topology faithfully mirrored a DAG that declared overlapping
    work as parallel roots.

    Slices with **disjoint** file sets are safe to branch in parallel off
    the shared base — that concurrency is the whole point of slicing — so
    only *overlapping, unordered* pairs are flagged. Two slices on the
    same chain are fine even when an intermediate slice is disjoint: the
    later branch is still cut transitively from the earlier one's tip.

    The forest constraint (:func:`validate_forest`: ≤1 DAG parent per
    slice) means the remediation is always to collapse the overlapping
    cluster into a single linear ``dependencies`` chain — you cannot
    express "depends on both A and B" as a diamond, so the architect
    picks an order and stacks them. Each error names the shared files so
    the architect's re-propose is actionable.

    ``files_affected`` is read from each slice's tasks (the same
    planner-declared signal :func:`validate_task_role_alignment` uses).
    Slices with no declared files contribute no overlap signal and are
    skipped. Cyclic / duplicate-id DAGs are reported separately by
    :func:`validate_forest`; the reachability walk here is cycle-safe so a
    cycle neither loops nor crashes this validator.

    Args:
        slices: The slice list extracted from the contract / plan.

    Returns:
        One structured-error string per offending unordered overlapping
        pair, in deterministic (declared-order) order. An empty list
        means no overlap-ordering violations.
    """

    # Deduplicate by id (duplicate ids are reported by validate_forest);
    # preserve declared order for deterministic pair iteration. Paths are
    # canonicalised (``posixpath.normpath`` + strip ``./``) so two slices
    # that name the same logical file in different surface forms — e.g.
    # ``orchestrator/x.py`` vs ``./orchestrator/x.py`` vs
    # ``a/b/../c.py`` vs ``a/c.py`` — collide as expected. Mirrors
    # :func:`_is_file_blocked_for_role`'s normalisation so plan-time
    # validation and the gateway's push-time check see the same paths.
    def _normalize(p: str) -> str:
        n = posixpath.normpath(p)
        if n.startswith("./"):
            n = n[2:]
        return n

    ordered_ids: list[str] = []
    files_by_id: dict[str, set[str]] = {}
    deps_by_id: dict[str, list[str]] = {}
    for slice_ in slices:
        if slice_.id in files_by_id:
            continue
        ordered_ids.append(slice_.id)
        files: set[str] = set()
        for task in slice_.tasks:
            for f in task.files_affected or []:
                files.add(_normalize(f))
        files_by_id[slice_.id] = files
        deps_by_id[slice_.id] = [d for d in (slice_.dependencies or []) if d]

    known = set(ordered_ids)

    # Transitive-ancestor set for each slice: every slice it depends on
    # directly or transitively, following ``dependencies`` edges. The
    # per-start visited set keeps the walk cycle-safe (a cyclic DAG is
    # reported by validate_forest, not here).
    def _ancestors(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [d for d in deps_by_id.get(start, []) if d in known]
        while stack:
            cur = stack.pop()
            if cur == start or cur in seen:
                continue
            seen.add(cur)
            stack.extend(d for d in deps_by_id.get(cur, []) if d in known)
        return seen

    ancestors = {sid: _ancestors(sid) for sid in ordered_ids}

    errors: list[str] = []
    for i, a in enumerate(ordered_ids):
        for b in ordered_ids[i + 1 :]:
            shared = files_by_id[a] & files_by_id[b]
            if not shared:
                continue
            # Ordered iff one is a transitive ancestor of the other.
            if b in ancestors[a] or a in ancestors[b]:
                continue
            errors.append(
                f"Slices '{a}' and '{b}' both touch {', '.join(sorted(shared))} "
                "but neither depends on the other; the implement phase branches "
                "each slice independently off the shared base, so their edits "
                "to the shared file(s) collide at integration (e.g. a "
                "modify/delete conflict). Order them along one dependency "
                "chain — add the earlier slice to the other's 'dependencies' "
                "so the later slice's branch is cut from it — or merge them "
                "into a single slice. See issue #3046."
            )
    return errors


def _is_file_blocked_for_role(role: str, file_path: str, repo: str | None = None) -> bool:
    """Return True if ``file_path`` is blocked for ``role`` per the role's
    ``AGENT_PATTERNS`` blocklist (with block-exempt carve-outs).

    Mirrors ``gateway/phase_filter.py::FileRestriction.is_file_blocked``
    so plan-time validation matches push-time enforcement 1:1. The
    gateway's check intentionally consults only blocked + block-exempt
    patterns (not allowed_patterns), and so does this function.

    Per-repo overrides (#2528): when ``repo`` is supplied, the pattern
    used reflects ``role_patterns:`` in ``repositories.yaml`` for that
    repo, so plan-time validation predicts push-time enforcement for
    non-Python repos too.
    """
    # AGENT_PATTERNS is imported lazily here to avoid a circular import:
    # egg_restrictions.patterns imports egg_contracts.agent_roles, which
    # triggers egg_contracts/__init__.py, which imports this module. A
    # module-scope import would deadlock that cycle and break the gateway
    # production boot path. egg_restrictions.matchers.match_pattern is
    # deliberately split out of patterns.py for safe module-scope use
    # (see matchers.py docstring); only the registry lookup needs to be lazy.
    from egg_restrictions.patterns import get_agent_pattern_for_repo

    pattern = get_agent_pattern_for_repo(role, repo=repo)
    if pattern is None:
        return False

    normalized = posixpath.normpath(file_path)
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith("../") or normalized.startswith("/"):
        return True

    if not any(match_pattern(normalized, p) for p in pattern.blocked_patterns):
        return False
    if any(match_pattern(normalized, p) for p in pattern.block_exempt_patterns):
        return False
    return True


def _eligible_producer_roles(files: list[str], repo: str | None = None) -> list[str]:
    """Return the producer roles (coder/tester/documenter) for which
    every file in ``files`` passes the gateway's blocked-pattern check.

    The result preserves the canonical coder→tester→documenter ordering
    so suggestions are deterministic across runs. Honours per-repo
    pattern overrides (#2528).
    """
    from ..agent_roles import AgentRole

    ordered_roles = (AgentRole.CODER, AgentRole.TESTER, AgentRole.DOCUMENTER)
    eligible: list[str] = []
    for role in ordered_roles:
        if all(not _is_file_blocked_for_role(role, f, repo=repo) for f in files):
            eligible.append(role.value)
    return eligible


def _check_role_files(task: Task, slice_id: str, repo: str | None = None) -> str | None:
    """Return a structured error string for a misaligned task, or
    ``None`` if the task's ``role`` can push every file in
    ``files_affected``.

    Per-task hook kept separate from the outer slice walk so the
    role-vs-files check can grow (per-repo overrides, role-specific
    exceptions) without restructuring ``validate_task_role_alignment``.
    The check delegates to ``_is_file_blocked_for_role``, which is the
    same predicate the gateway enforces at push time — so plan-time
    rejections match push-time rejections.

    Tasks without a ``role`` or with empty ``files_affected`` return
    ``None`` — the parser already treats ``role`` as optional, and an
    empty file list leaves nothing to check (prose/research tasks).
    """
    role = task.role
    files = list(task.files_affected or [])
    if not role or not files:
        return None
    blocked = [f for f in files if _is_file_blocked_for_role(role, f, repo=repo)]
    if not blocked:
        return None
    eligible = _eligible_producer_roles(files, repo=repo)
    if len(eligible) == 1:
        hint = f"Reassign to role '{eligible[0]}' — it can push every file in this task."
    elif len(eligible) > 1:
        hint = (
            f"Eligible roles for this file set: {eligible}. "
            "Pick one and update the task's 'role' field."
        )
    else:
        hint = (
            "No producer role can push every file in this task. Either "
            "split the task so each subtask falls within a single "
            "role's scope, or — for `.github/` files — stage them "
            "under top-level `.github-staging/` and call them out in "
            "the PR body for the human reviewer (issue #2508)."
        )
    return (
        f"Task '{task.id}' (slice '{slice_id}') is assigned role "
        f"'{role}' but files {blocked} are blocked for that role per "
        f"shared/egg_restrictions/patterns.py. {hint}"
    )


def validate_task_role_alignment(slices: list[Slice], repo: str | None = None) -> list[str]:
    """Walk the slice/task tree and reject tasks whose ``role`` cannot
    push their ``files_affected``.

    Added in #2527. The plan-phase ``task_planner`` can assign tasks to
    producer roles whose ``shared/egg_restrictions/patterns.py``
    blocklist forbids the listed files; the mismatch is otherwise only
    caught at push time by the gateway's
    ``check_file_restrictions``, which means the producer agent gets
    spawned, explores, sometimes builds workarounds, and only then
    hits ``403 restricted_path_modified``. Running the same check
    at plan time lets the plan reviewer NACK the planner before any
    producer cycle is wasted.

    Per-task logic lives in ``_check_role_files`` so future
    role-vs-files exceptions can be added without restructuring this
    outer walk.

    Args:
        slices: The slice list extracted from the contract / plan.
        repo: Optional ``owner/repo`` for per-repo pattern overrides
            (#2528). When set, the validator uses the repo's
            ``role_patterns:`` block from ``repositories.yaml`` so
            plan-time validation predicts push-time enforcement on
            non-Python repos. When ``None``, falls back to the global
            default patterns.

    Returns:
        A list of structured-error strings — one entry per offending
        task. Each entry names the task ID, the assigned role, the
        blocked files, and the eligible-role hint so the plan reviewer
        can surface an actionable NACK reason.
    """
    errors: list[str] = []
    for slice_ in slices:
        for task in slice_.tasks:
            err = _check_role_files(task, slice_.id, repo=repo)
            if err is not None:
                errors.append(err)
    return errors


def validate_plan_preflight(content: str) -> None:
    """AC-1a plan-phase pre-flight validator (#2777).

    Runs at plan-phase completion (before the implement-phase entry hook
    fires) and rejects malformed planner output that the new idempotent
    context-PR opener — :func:`orchestrator.routes.pipelines._open_context_pr_at_implement_start`
    — depends on. The opener needs ``contract.pr.title`` and
    ``contract.pr.description`` set; the populate-from-plan step that
    writes those fields needs a parseable ``# yaml-tasks`` block; and a
    well-formed PR record means a useful PR body for human reviewers,
    so we also reject missing ``test_plan`` / ``manual_steps`` here
    rather than catching them downstream as silent contract gaps.

    Required rejections (each adds one entry to ``missing_fields``):

    (a) ``yaml-tasks`` — block missing or unparseable
        (``parse_plan`` returns ``success=False`` or finds no phases).
    (b) ``pr.title`` — missing or empty after whitespace strip.
    (c) ``pr.description`` — missing or empty after whitespace strip.
    (d) ``pr.test_plan`` — missing or empty after whitespace strip.
    (e) ``pr.manual_steps`` — key missing entirely (empty string is
        allowed; the contract field defaults to ``""``).

    Raises:
        PlanPreflightError: When one or more required fields are
            missing. ``missing_fields`` is an ordered list of field
            names ``["yaml-tasks", "pr.title", ...]``. Empty
            ``content`` surfaces as ``["yaml-tasks"]`` rather than a
            separate field because callers downstream treat both
            cases identically — there is no parseable plan.

    Ordering note: callers should invoke this BEFORE
    :func:`_populate_contract_from_plan_safe` so the rejection lands
    as a typed 422 / NACK rather than the populate path's silent
    warn-log. The orchestrator wires this into
    ``routes/phases.py``'s plan→implement advance block.
    """
    missing: list[str] = []
    detail: str | None = None

    # (a) yaml-tasks block must be present and parseable. ``parse_plan``
    # already implements the three-tier parsing strategy; we treat any
    # failure mode (empty content, no phases, parse error) as a missing
    # yaml-tasks block from the validator's point of view.
    result = _pkg.parse_plan(content)
    if not result.success or not result.phases:
        missing.append("yaml-tasks")
        if result.error:
            detail = result.error

    # (b)–(e) PR metadata fields. Even if (a) failed, surface the
    # remaining missing fields so the operator sees the full picture in
    # one NACK message — re-running validation after fixing yaml-tasks
    # only to discover a missing test_plan is a wasted cycle.
    pr_title = (result.pr_title or "").strip()
    if not pr_title:
        missing.append("pr.title")

    pr_description = (result.pr_description or "").strip()
    if not pr_description:
        missing.append("pr.description")

    pr_test_plan = (result.pr_test_plan or "").strip()
    if not pr_test_plan:
        missing.append("pr.test_plan")

    # (e) manual_steps: an empty string IS allowed (contract default),
    # so we only reject when the key is ABSENT from the parsed YAML
    # entirely. ``ParseResult.pr_manual_steps`` cannot distinguish
    # "key missing" from "key present with empty value" because
    # ``extract_pr_metadata_from_yaml`` normalises both via
    # ``_normalize_optional_string`` which maps ``None`` → ``""``.
    # Inspect ``raw_yaml`` directly so the key-presence check is
    # structural rather than value-shape-dependent (reviewer_code v2
    # NACK blocker 1).
    raw_pr_block: dict[str, Any] = {}
    if isinstance(result.raw_yaml, dict):
        candidate = result.raw_yaml.get("pr")
        if isinstance(candidate, dict):
            raw_pr_block = candidate
    if "manual_steps" not in raw_pr_block:
        missing.append("pr.manual_steps")

    if missing:
        raise PlanPreflightError(missing, detail=detail)
