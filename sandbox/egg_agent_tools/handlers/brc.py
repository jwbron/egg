"""BRC consensus handlers (propose, ack, nack, confirm, state, blocking, peer read)."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from egg_agent_tools.handlers._gateway import (
    _SLICE_ID_PATTERN,
    get_agent_role,
    get_pipeline_id,
    get_slice_id,
    orchestrator_request,
)
from egg_agent_tools.handlers._gateway import maybe_attach_slice_id as _maybe_attach_slice_id
from egg_agent_tools.handlers.errors import GatewayError, HandlerError

_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
_PIPELINE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_commit_sha(sha: str) -> str:
    if not _COMMIT_SHA_PATTERN.match(sha):
        raise HandlerError(f"Invalid commit SHA '{sha}': expected 7-40 hexadecimal characters")
    return sha


def _require_pipeline_id(req: dict[str, Any]) -> str:
    pid = req.get("pipeline_id") or get_pipeline_id()
    if not pid:
        raise HandlerError("pipeline_id required. Set EGG_PIPELINE_ID or pass 'pipeline_id'.")
    if not _PIPELINE_ID_PATTERN.match(pid):
        raise HandlerError(f"Invalid pipeline_id {pid!r}: must match [a-zA-Z0-9_-]+")
    return pid


def _require_role(req: dict[str, Any]) -> str:
    role = req.get("role") or get_agent_role()
    if not role:
        raise HandlerError("role required. Set EGG_AGENT_ROLE or pass 'role'.")
    return role


def _require_version_int(req: dict[str, Any], key: str) -> int:
    """Require an integer version field on the request.

    The producer's current proposal version must be plumbed through ACK / NACK
    so the orchestrator's version-match guard can detect stale verdicts and
    reject with a structured 409 (#2142).  Without this, the guard's fallback
    silently passes whenever the caller omits the field.

    Enforces ``version >= 1`` because v0 is meaningless: there is no proposal
    to ACK / NACK before the producer's first ``CONSENSUS_PROPOSE``.  Catching
    this at the handler boundary surfaces a callers-confused-the-units bug
    before the request hits the wire.
    """
    raw = req.get(key)
    if raw is None:
        raise HandlerError(
            f"'{key}' is required (the producer's current proposal version "
            "you reviewed; read it from the CONSENSUS_PROPOSE message)"
        )
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise HandlerError(f"'{key}' must be an integer; got {raw!r}") from exc
    if version < 1:
        raise HandlerError(f"'{key}' must be >= 1; got {version} (v0 means no proposal exists yet)")
    return version


def _resolve_head_sha() -> str:
    cwd = os.environ.get("EGG_REPO_PATH") or None
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            cwd=cwd,
            # stdin=DEVNULL defensively avoids the subprocess inheriting
            # a non-tty parent's stdio and blocking on an interactive
            # prompt.  Covers the edge case where the handler runs
            # inside a cron / systemd-style parent.
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise HandlerError("'commit_sha' not provided and could not resolve HEAD") from exc


# Pydantic v2's lax bool coercion accepts these string forms (case-insensitive).
# Mirrored here so pre-flight matches the orchestrator's parse step on
# tests_execution_blocked — see _coerce_attestation_bool below.
_BOOL_TRUE_STRINGS = frozenset({"true", "yes", "on", "1", "t", "y"})
_BOOL_FALSE_STRINGS = frozenset({"false", "no", "off", "0", "f", "n"})


def _coerce_attestation_bool(value: Any, *, field: str) -> bool:
    """Coerce a JSON-ish value to bool, matching Pydantic v2's lax rules.

    Used for the tester attestation's bool fields — ``tests_execution_blocked``
    and (since #2431) ``no_test_changes_needed`` — so pre-flight's verdict
    matches the orchestrator's Pydantic parse step. Without this, a string
    like ``"false"`` is truthy under Python's ``bool()`` (non-empty string)
    but parses to ``False`` in Pydantic — pre-flight would reject a
    payload the orchestrator would accept.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE_STRINGS:
            return True
        if normalized in _BOOL_FALSE_STRINGS:
            return False
    raise HandlerError(
        f"Tester attestation: '{field}' must be a bool (got {value!r}). "
        "Pass true/false, or one of the string forms 'true'/'false'/"
        "'yes'/'no'/'on'/'off'/'1'/'0'."
    )


def _coerce_attestation_int(value: Any, *, field: str) -> int:
    """Coerce a JSON-ish value to int, matching Pydantic v2's lax rules.

    Used for ``tests_run`` so pre-flight's verdict matches the
    orchestrator's Pydantic parse step. The strict-mode rule logic only
    runs *after* Pydantic has parsed the payload into a typed model;
    payloads that fail parsing (lists, unparseable strings,
    non-integer-valued floats) raise ``ValidationError`` and never
    reach ``_validate_strict``. Without this helper, pre-flight's
    blocked branch would silently swallow a bad ``tests_run`` and let
    the request go on the wire — which is exactly what the orchestrator
    rejects at parse time.

    Accepts: ``int`` (incl. ``bool``, since ``bool`` is an ``int``
    subclass and Pydantic v2 lax-mode accepts it), ``float`` only when
    ``is_integer()`` is true, and ``str`` only when stripping yields a
    parseable integer. Rejects everything else with ``HandlerError``.
    """
    if isinstance(value, bool):
        # bool is an int subclass; Pydantic v2 lax mode accepts True/False
        # for an int field (coerces to 1/0).
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise HandlerError(
                f"Tester attestation: '{field}' must be an integer count "
                f"of tests executed (e.g. 42). Got {value!r}, a "
                "non-integer float — Pydantic only accepts integer-valued "
                "floats here."
            )
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise HandlerError(
                f"Tester attestation: '{field}' must be an integer count "
                f"of tests executed (e.g. 42). Got {value!r}, an "
                "unparseable string. Note: this is the attestation field, "
                "distinct from the propose tool's top-level tests_run "
                "argument (which carries test *identifiers* as a list of "
                "strings)."
            ) from exc
    raise HandlerError(
        f"Tester attestation: '{field}' must be an integer count of "
        f"tests executed (e.g. 42). Got {value!r}. Note: this is the "
        "attestation field, distinct from the propose tool's top-level "
        "tests_run argument (which carries test *identifiers* as a "
        "list of strings)."
    )


def _validate_tester_attestation_pre_flight(attestation: dict[str, Any]) -> None:
    """Catch missing / malformed tester attestation at the handler boundary (#2338).

    Pre-flight has two layers, both mirroring the orchestrator:

    1. **Type checks** that mirror Pydantic v2's parse step on
       ``TesterAttestation`` (``orchestrator/attestation_schemas.py``).
       Runs unconditionally — a payload that would fail
       ``model_cls(**attestation_data)`` (e.g. ``tests_run="abc"``,
       ``tests_run=["t1"]``, ``tests_run=0.5``, ``checks_passed="lint"``)
       fails pre-flight with the same verdict, regardless of whether
       ``tests_execution_blocked`` is true. This catches divergence #1
       from PR #2344's re-review: the orchestrator's Pydantic parse
       rejects bad types in both branches, so pre-flight must too.
    2. **Strict-mode rule logic** that mirrors ``_validate_strict``
       (the post-Pydantic checks). Branches on the parsed
       ``tests_execution_blocked`` and ``no_test_changes_needed`` values
       to enforce the ``tests_run > 0 + checks_passed populated``
       requirement, or one of the alternative paths:

       - ``tests_execution_blocked=true`` + non-empty
         ``tests_execution_blocked_reason`` (checks could not run);
       - ``no_test_changes_needed=true`` + non-empty
         ``no_test_changes_reason`` + populated ``checks_passed`` —
         the no-op propose path for refactor / doc-only slices (#2431).

    Failures raise ``HandlerError`` with an actionable message
    naming the field, the common cause, and the expected format —
    including a callout disambiguating the attestation's ``tests_run``
    (integer count) from the propose tool's top-level ``tests_run``
    (list of test identifiers). The orchestrator's check stays — this
    is defense-in-depth, not a replacement.

    Pre-flight enforces strict-mode rules unconditionally, regardless of
    the pipeline's ``attestation_strictness`` setting. The handler has no
    cheap way to read tracker strictness from the sandbox, and a relaxed
    pipeline that has been reconstructed post-restart
    (``orchestrator/peer_consensus.py`` keeps reconstructed trackers in
    ``RELAXED`` for the rest of their lifetime) will see pre-flight reject
    proposals the orchestrator would have accepted. That is acceptable:
    the canonical "tester forgot to populate attestation.tests_run" case
    from #2338 is the same misconfiguration in both modes, so failing
    fast with an actionable error is better UX than letting an empty
    payload silently slip past in relaxed mode.

    Invariant: pre-flight verdict (accept / raise) mirrors
    ``validate_attestation(role="tester", strictness=STRICT)`` for the
    canonical #2338 footguns — empty payload, missing/zero/non-int
    ``tests_run``, missing/empty/non-list/non-string-item
    ``checks_passed``, and the lax bool/int string forms Pydantic v2
    accepts. Enforced by
    ``test_handlers_brc.py::TestPreFlightMirrorsOrchestrator``; if you
    tighten one side on a covered rule, tighten both.

    Known inverse-direction divergences (pre-flight is stricter than
    the orchestrator on these — agents see a friendlier HandlerError
    locally instead of the orchestrator accepting an obscure type):

    - ``checks_passed`` as a tuple (Pydantic coerces to list).
    - ``tests_execution_blocked`` and ``no_test_changes_needed`` as
      ``0.0``/``1.0`` float, ``b"true"`` bytes, or other Pydantic-lax
      bool inputs not in ``_BOOL_TRUE_STRINGS`` /
      ``_BOOL_FALSE_STRINGS`` — both fields share
      ``_coerce_attestation_bool``, so the divergence applies
      symmetrically.
    - ``tests_run`` as a stringified non-integer float (e.g. ``"1.0"``)
      — Pydantic v2 accepts via float-then-int; pre-flight requires
      the string to parse straight to int.

    JSON-deserialised payloads from the wire don't produce these types
    in practice (no tuples, no bytes), so the worst case is a clearer
    error on the producer side. The orchestrator's strict validator
    stays as defense-in-depth for any payload that does slip through.

    Known same-direction divergence (academic, both reject in
    practice): ``tests_run = 1e20`` — Pydantic v2's int field has
    overflow protection, ``int(1e20)`` does not. Producers don't
    construct counts this large.
    """
    # Layer 1 — type checks that mirror Pydantic's parse step. Runs
    # unconditionally. A payload that would bounce off
    # ``model_cls(**attestation_data)`` bounces off pre-flight first
    # with a friendlier message.
    blocked = _coerce_attestation_bool(
        attestation.get("tests_execution_blocked", False),
        field="tests_execution_blocked",
    )
    no_test_changes = _coerce_attestation_bool(
        attestation.get("no_test_changes_needed", False),
        field="no_test_changes_needed",
    )
    tests_run_int = _coerce_attestation_int(
        attestation.get("tests_run", 0),
        field="tests_run",
    )
    checks_passed = attestation.get("checks_passed", [])
    if not isinstance(checks_passed, list):
        # Pydantic rejects ``checks_passed`` if it isn't a list of
        # strings. Mirror the type check unconditionally; the
        # populated-ness check below only runs in non-blocked mode
        # (Pydantic-parsed empty list is fine when blocked).
        raise HandlerError(
            "Tester attestation: 'checks_passed' must be a list of strings "
            f"(e.g. ['lint', 'test']). Got {checks_passed!r}."
        )
    # Item-type check: Pydantic's ``list[str]`` field rejects payloads
    # like ``checks_passed=[1, 2, 3]`` — exactly the #2338 footgun
    # shape (agent forgets to stringify). Without this, pre-flight
    # accepts and the request bounces off the orchestrator as a 400.
    bad_items = [item for item in checks_passed if not isinstance(item, str)]
    if bad_items:
        raise HandlerError(
            "Tester attestation: 'checks_passed' items must be strings "
            f"(e.g. ['lint', 'test']). Got non-string items: {bad_items!r}. "
            "Stringify check identifiers before passing them in."
        )

    # Layer 2 — strict-mode rule logic that mirrors _validate_strict.
    # Mutual exclusion (#2431): blocked vs. no-changes-needed describe
    # different failure modes and cannot both be true.
    if blocked and no_test_changes:
        raise HandlerError(
            "Tester attestation: tests_execution_blocked and "
            "no_test_changes_needed are mutually exclusive. Pick one: "
            "'blocked' means the configured checks could not run; "
            "'no_test_changes_needed' means they ran and passed but the "
            "slice warrants no new tests."
        )
    if blocked:
        reason = (attestation.get("tests_execution_blocked_reason") or "").strip()
        if not reason:
            raise HandlerError(
                "Tester attestation: 'tests_execution_blocked' is true but "
                "'tests_execution_blocked_reason' is empty. Populate "
                "attestation.tests_execution_blocked_reason with why tests "
                "could not run (e.g. 'private-network mode blocked package "
                "downloads'), then retry."
            )
        # Mutual-exclusion guard: blocked pipelines must not also report
        # tests_run > 0 — pick one and stick with it.
        if tests_run_int > 0:
            raise HandlerError(
                "Tester attestation: tests_execution_blocked=true conflicts "
                "with tests_run > 0. If some tests ran, set "
                "tests_execution_blocked=false and report the count and "
                "checks_passed normally."
            )
        return

    if no_test_changes:
        # No-op propose path for refactor / doc-only slices (#2431).
        # tests_run == 0 is allowed; checks_passed is still required
        # below (the tester ran the configured checks).
        no_changes_reason = (attestation.get("no_test_changes_reason") or "").strip()
        if not no_changes_reason:
            raise HandlerError(
                "Tester attestation: 'no_test_changes_needed' is true but "
                "'no_test_changes_reason' is empty. Populate "
                "attestation.no_test_changes_reason with why the slice "
                "warrants no new tests (e.g. 'pure refactor: symbol "
                "moves, no behavior change; existing test coverage "
                "applies'), then retry."
            )
    elif tests_run_int == 0:
        # Non-blocked, non-no-op path — strict mode requires
        # tests_run > 0. Mirror the orchestrator: only ``tests_run == 0``
        # is rejected. Negative counts slip past Pydantic (no constraint
        # on the int field) and the strict validator's ``elif
        # instance.tests_run == 0`` check, so pre-flight intentionally
        # lets them pass too. If we tighten one side, tighten both — see
        # ``test_handlers_brc.py::TestPreFlightMirrorsOrchestrator``.
        raise HandlerError(
            "Tester attestation requires tests_run > 0 (the integer count "
            "of tests executed). If tests genuinely could not run, set "
            "tests_execution_blocked=true with a non-empty "
            "tests_execution_blocked_reason. If the slice is a pure "
            "refactor or doc-only change with no new tests warranted, "
            "set no_test_changes_needed=true with a non-empty "
            "no_test_changes_reason instead. Pass these as fields on the "
            "propose tool's `attestation` dict — e.g. "
            "attestation={'tests_run': 42, 'checks_passed': "
            "['lint', 'test']}."
        )

    if not checks_passed:
        raise HandlerError(
            "Tester attestation requires checks_passed to list the "
            "configured checks that actually passed (e.g. "
            "['lint', 'test']). Only include checks that passed — do NOT "
            "include checks that failed. Empty list is rejected unless "
            "tests_execution_blocked is true."
        )


def brc_propose(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_PROPOSE signal.

    Request (all optional unless noted):
        summary (str): proposal summary (required; ≥50 chars recommended)
            unless ``raw_payload`` already carries it.
        artifacts (list[str]): artifact references.
        risk_considered (str): risk summary.
        commit_sha (str): commit SHA; defaults to ``git rev-parse HEAD``.
        files_changed (list[str])
        tests_run (list[str]): test *identifiers* executed (e.g. test
            node IDs). Distinct from the attestation's ``tests_run``
            field, which is an integer count of tests executed (see
            ``attestation`` below).
        tasks (list[str]): tasks_satisfied
        attestation (dict): role-specific attestation payload. For the
            ``tester`` role under strict mode, requires one of:
            (a) ``tests_run > 0`` (integer) and a non-empty
            ``checks_passed`` list (the normal path);
            (b) ``tests_execution_blocked=true`` with a non-empty
            ``tests_execution_blocked_reason`` (checks could not run);
            (c) ``no_test_changes_needed=true`` with a non-empty
            ``no_test_changes_reason`` and populated ``checks_passed``
            — the no-op propose path for refactor / doc-only slices
            where the configured checks ran and passed but no new
            tests were warranted (#2431). Pre-flight validated by this
            handler so misconfigurations fail locally rather than
            bouncing off the orchestrator as 400 (#2338).
        changed_artifacts (list[str]): optional re-proposal delta.
        raw_payload (dict): pre-built payload dict — every key is
            forwarded verbatim to the orchestrator.  Structured
            ``req`` keys take precedence when both are supplied.
        pipeline_id, role: contract/role overrides.

    Response:
        { ok: True, signal: <orchestrator response>, phase: "..." }
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)

    raw_payload = req.get("raw_payload")
    if raw_payload and not isinstance(raw_payload, dict):
        raise HandlerError("'raw_payload' must be a dict if provided")

    summary = req.get("summary") or (raw_payload.get("summary") if raw_payload else None)
    if not summary or not isinstance(summary, str):
        raise HandlerError("'summary' is required")

    user_sha = req.get("commit_sha") or (raw_payload.get("commit_sha") if raw_payload else None)
    if user_sha:
        _validate_commit_sha(user_sha)
    commit_sha = user_sha or _resolve_head_sha()

    # Start from raw_payload (if any) so unknown/custom schema fields
    # are preserved verbatim; structured kwargs layer on top.
    payload: dict[str, Any] = dict(raw_payload) if raw_payload else {}
    payload.update(
        {
            "summary": summary,
            "attestation": req.get("attestation") or payload.get("attestation") or {},
            "artifacts": list(req.get("artifacts") or payload.get("artifacts") or []),
            "risk_considered": (
                req.get("risk_considered")
                or req.get("risk")
                or payload.get("risk_considered")
                or payload.get("risk")
                or ""
            ),
            "commit_sha": commit_sha,
            "files_changed": list(req.get("files_changed") or payload.get("files_changed") or []),
            "tests_run": list(req.get("tests_run") or payload.get("tests_run") or []),
            "tasks_satisfied": list(
                req.get("tasks")
                or req.get("tasks_satisfied")
                or payload.get("tasks_satisfied")
                or payload.get("tasks")
                or []
            ),
        }
    )
    # Pre-flight role-specific attestation validation (#2338). Mirrors
    # the orchestrator's strict-mode checks so misconfigurations fail
    # at the handler boundary with an actionable HandlerError instead
    # of going on the wire and bouncing back as a 400. Strict-mode-only
    # — the orchestrator gates this; relaxed-mode pipelines won't reach
    # the strict validator and aren't disrupted by pre-flight here.
    if role == "tester" and isinstance(payload.get("attestation"), dict):
        _validate_tester_attestation_pre_flight(payload["attestation"])

    data: dict[str, Any] = {
        "signal_type": "consensus_propose",
        "agent_role": role,
        "payload": payload,
    }
    if req.get("changed_artifacts"):
        data["changed_artifacts"] = list(req["changed_artifacts"])
    _maybe_attach_slice_id(req, data)

    try:
        result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    except GatewayError as exc:
        # Open-NACK barrier (#2142): the orchestrator returns 409 with a
        # structured envelope when re-propose is blocked because NACKs
        # against the current version haven't been delivered to the
        # producer yet.  Surface it as structured return data so the
        # agent can read the inlined NACK list and aggregate fixes
        # without parsing stderr.
        if (
            getattr(exc, "status_code", None) == 409
            and isinstance(exc.details, dict)
            and exc.details.get("status") == "open_nacks_blocked"
        ):
            return {
                "ok": False,
                "role": role,
                "status": "open_nacks_blocked",
                "message": exc.message,
                "rejection": exc.details,
            }
        raise
    if not result.get("success"):
        raise GatewayError(result.get("message", "propose failed"))

    consensus = result.get("data", {}).get("consensus", {})
    phase = consensus.get("agents", {}).get(role, {}).get("phase", "")
    return {"ok": True, "role": role, "phase": phase, "signal": result}


def brc_ack(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_ACK signal for a producer.

    Request:
        producer_role (str): required.
        reason (str): required.
        files_reviewed (list[str]): optional list of artifact references.
        pre_merge_condition (str): optional. Turns this into a **conditional
            ACK** — the work is approved but a human must perform the named
            action before merging (e.g. "git mv old/path new/path"). Surfaces
            as a dedicated "Pre-merge Obligations" section on the auto-created
            PR so the merger sees it instead of skimming past a prose note in
            the ACK reason (#1998).
        pre_merge_condition_resolved_in_diff (str): optional commit SHA. Set
            on a re-ACK when ``pre_merge_condition`` has been satisfied within
            the same PR's diff since the initial conditional ACK — the PR-body
            renderer demotes resolved obligations to a "Resolved within this
            PR" subsection so reviewers don't see merge-blocking boilerplate
            on busywork (#2336). Only meaningful alongside a non-empty
            ``pre_merge_condition``.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    producer_role = req.get("producer_role")
    if not producer_role:
        raise HandlerError("'producer_role' is required")
    reason = req.get("reason")
    if not reason:
        raise HandlerError("'reason' is required")
    ack_version = _require_version_int(req, "ack_version")

    payload: dict[str, Any] = {
        "artifact_references": list(req.get("files_reviewed") or []),
        "reason": reason,
        "ack_version": ack_version,
    }
    pre_merge_condition = req.get("pre_merge_condition") or ""
    resolved_in_diff = req.get("pre_merge_condition_resolved_in_diff") or ""
    # Thread both fields through unconditionally so the signal-layer
    # validator can reject a resolution-without-condition request rather
    # than silently dropping the SHA at the MCP boundary (#2336 review).
    if pre_merge_condition:
        payload["pre_merge_condition"] = pre_merge_condition
    if resolved_in_diff:
        payload["pre_merge_condition_resolved_in_diff"] = resolved_in_diff

    data = {
        "signal_type": "consensus_ack",
        "agent_role": role,
        "producer_role": producer_role,
        "payload": payload,
    }
    _maybe_attach_slice_id(req, data)
    try:
        result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    except GatewayError as exc:
        # Stale-version rejection (#2142): the orchestrator returns 409
        # with the producer's current proposal snapshot inlined when the
        # ACK targeted a superseded version.  Surface it as structured
        # data so the reviewer can re-fetch and re-review without
        # parsing stderr.
        if (
            getattr(exc, "status_code", None) == 409
            and isinstance(exc.details, dict)
            and exc.details.get("status") == "stale_version"
        ):
            return {
                "ok": False,
                "role": role,
                "producer_role": producer_role,
                "status": "stale_version",
                "message": exc.message,
                "rejection": exc.details,
            }
        raise
    if not result.get("success"):
        raise GatewayError(result.get("message", "ack failed"))
    return {"ok": True, "role": role, "producer_role": producer_role, "signal": result}


def brc_nack(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_NACK signal for a producer.

    Request:
        producer_role (str): required.
        reason (str): required (describes why the proposal is blocked).
        files_reviewed (list[str]): optional list of artifact references.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    producer_role = req.get("producer_role")
    if not producer_role:
        raise HandlerError("'producer_role' is required")
    reason = req.get("reason")
    if not reason:
        raise HandlerError("'reason' is required")
    nack_version = _require_version_int(req, "nack_version")

    data = {
        "signal_type": "consensus_nack",
        "agent_role": role,
        "producer_role": producer_role,
        "payload": {
            "reason": reason,
            "artifact_references": list(req.get("files_reviewed") or []),
            "nack_version": nack_version,
        },
    }
    _maybe_attach_slice_id(req, data)
    try:
        result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    except GatewayError as exc:
        # Stale-version rejection (#2142): same envelope as ACK.  See
        # ``brc_ack`` for full rationale.
        if (
            getattr(exc, "status_code", None) == 409
            and isinstance(exc.details, dict)
            and exc.details.get("status") == "stale_version"
        ):
            return {
                "ok": False,
                "role": role,
                "producer_role": producer_role,
                "status": "stale_version",
                "message": exc.message,
                "rejection": exc.details,
            }
        raise
    if not result.get("success"):
        raise GatewayError(result.get("message", "nack failed"))
    return {"ok": True, "role": role, "producer_role": producer_role, "signal": result}


def brc_confirm(req: dict[str, Any]) -> dict[str, Any]:
    """Send CONSENSUS_CONFIRMED after all reviewers have ACKed.

    Request:
        pipeline_id, role: overrides.

    Response carries:
        ok: True only when the producer transitioned to CONFIRMED.
            False for "pending_acks" — the orchestrator received the
            request but rejected the transition (e.g.
            ``producer_not_fully_acked``, ``global_zero_proposal``,
            ``stale_acks``). Inspect ``status`` and ``message`` to pick
            corrective action.
        status: "confirmed"|"pending_acks"
        consensus_reached: bool (only for status=="confirmed")
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)

    data: dict[str, Any] = {
        "signal_type": "consensus_confirmed",
        "agent_role": role,
    }
    _maybe_attach_slice_id(req, data)
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    if not result.get("success"):
        raise GatewayError(result.get("message", "confirm failed"))
    body = result.get("data", {})
    pending = body.get("status") == "pending_acks"
    return {
        "ok": not pending,
        "role": role,
        "status": "pending_acks" if pending else "confirmed",
        "consensus_reached": bool(body.get("consensus_reached", False)),
        "message": result.get("message"),
        "signal": result,
    }


def brc_get_state(req: dict[str, Any]) -> dict[str, Any]:
    """Fetch the current BRC consensus state for the pipeline.

    Request:
        pipeline_id: override.
        verbose (bool): include the full orchestrator status payload.

    Response:
        { ok: True, consensus: {...}, verbose: bool }

    No CLI counterpart — BRC state is a derived view of the pipeline
    status endpoint; the raw JSON is available via `egg-orch pipeline
    status --json` but the scraping rules are an agent-convenience
    shape unique to this tool (decision-13).
    """
    pid = _require_pipeline_id(req)
    verbose = bool(req.get("verbose", False))
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    data = result.get("data", {})
    consensus = data.get("concurrent", {}).get("consensus", {})

    response: dict[str, Any] = {
        "ok": True,
        "consensus": consensus,
        "is_complete": bool(consensus.get("is_complete", False)),
        "blocking_agents": list(consensus.get("blocking_agents", []) or []),
    }
    if verbose:
        response["raw"] = data
    return response


def brc_list_blocking(req: dict[str, Any]) -> dict[str, Any]:
    """Return the list of agent roles currently blocking consensus.

    Request:
        pipeline_id: override.

    No CLI counterpart — the same data is reachable via `egg-orch
    pipeline status --json` but the filtered blocking-agents shape is
    an agent-convenience view (decision-13).
    """
    pid = _require_pipeline_id(req)
    result = orchestrator_request(f"/api/v1/pipelines/{pid}/status")
    consensus = result.get("data", {}).get("concurrent", {}).get("consensus", {})
    blocking = list(consensus.get("blocking_agents", []) or [])
    return {"ok": True, "blocking_agents": blocking}


def brc_resolve_obligation(req: dict[str, Any]) -> dict[str, Any]:
    """Send a CONSENSUS_RESOLVE_OBLIGATION signal (#2338).

    Mark a reviewer's conditional-ACK obligation as satisfied in-cycle —
    typically called by the tester after cherry-picking work the coder
    is gateway-blocked from. The matrix keeps the obligation text for
    audit but ``get_pre_merge_conditions`` filters out resolved entries,
    so the PR body and HITL gate stop surfacing the obligation.

    Resolution is per-version: any later ACK / NACK / invalidate on the
    same edge resets the resolved flag (the new ACK gets a fresh
    obligation lifecycle). If the same obligation re-appears on a later
    proposal, call this signal again — or, preferred, have the reviewer
    drop the obligation when the conditioning work has landed in-cycle
    (see ``code-review-criteria.md``).

    No CLI counterpart: this is a net-new capability added in #2338
    (decision-13 rationale). Operators don't need a Bash entrypoint —
    the in-cycle obligation-resolution flow is producer/tester-driven
    via the MCP tool surface.

    Request:
        reviewer_role (str): required. The reviewer whose conditional-ACK
            you are marking resolved.
        producer_role (str): required. The producer the conditional-ACK
            was attached to.
        commit_sha (str): optional. Commit that satisfies the obligation;
            recorded for audit.
        note (str): optional. Free-form note explaining how the obligation
            was satisfied; surfaces in the audit log.
        pipeline_id, role: overrides.
    """
    pid = _require_pipeline_id(req)
    role = _require_role(req)
    reviewer_role = req.get("reviewer_role")
    if not reviewer_role:
        raise HandlerError("'reviewer_role' is required")
    producer_role = req.get("producer_role")
    if not producer_role:
        raise HandlerError("'producer_role' is required")
    commit_sha = (req.get("commit_sha") or "").strip()
    if commit_sha:
        _validate_commit_sha(commit_sha)
    note = (req.get("note") or "").strip()

    data: dict[str, Any] = {
        "signal_type": "consensus_resolve_obligation",
        "agent_role": role,
        "reviewer_role": reviewer_role,
        "producer_role": producer_role,
    }
    if commit_sha:
        data["commit_sha"] = commit_sha
    if note:
        data["note"] = note
    _maybe_attach_slice_id(req, data)

    result = orchestrator_request(f"/api/v1/pipelines/{pid}/signal", method="POST", data=data)
    if not result.get("success"):
        raise GatewayError(result.get("message", "resolve_obligation failed"))
    return {
        "ok": True,
        "role": role,
        "reviewer_role": reviewer_role,
        "producer_role": producer_role,
        "signal": result,
    }


_VALID_PHASES = ("refine", "plan", "implement", "pr")

# BRC message_type values that the orchestrator writes into the
# ``.egg-state/brc-history/<pipeline>-<phase>.json`` companion file.
# Mirrors orchestrator.routes.pipelines.BRC_HISTORY_TYPES; kept as a
# local tuple so the handler can validate `message_type` filters
# without importing the orchestrator package (which pulls fastapi).
_BRC_HISTORY_TYPES: frozenset[str] = frozenset(
    {
        "CONSENSUS_PROPOSE",
        "CONSENSUS_ACK",
        "CONSENSUS_NACK",
        "CONSENSUS_CONFIRMED",
        "CONSENSUS_RE_REVIEW",
        "CONSENSUS_WITHDRAWN",
    }
)

# peer_role / role slugs must be simple identifiers so the handler can
# never be tricked into path-traversal when future refactors embed the
# role into a filename (risk_analyst R2 hardening).
_ROLE_SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def _encode_cursor(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> dict[str, Any]:
    if cursor is None:
        return {"offset": 0, "skipped_malformed": 0}
    if not isinstance(cursor, str):
        raise HandlerError("'cursor' must be a string if provided")
    # Add back URL-safe base64 padding that was stripped on encode.
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding)
        data = json.loads(raw.decode())
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HandlerError(f"Invalid cursor: {cursor!r}") from exc
    if not isinstance(data, dict):
        raise HandlerError(f"Invalid cursor: {cursor!r}")
    offset = int(data.get("offset", 0))
    if offset < 0:
        raise HandlerError(f"Invalid cursor offset {offset}; must be >= 0")
    skipped = int(data.get("skipped_malformed", 0))
    if skipped < 0:
        skipped = 0
    return {"offset": offset, "skipped_malformed": skipped}


def _resolve_env_identifier_for_brc_history() -> str:
    """Resolve the filename-identifier from the env; NEVER accept a caller override.

    The orchestrator always writes the file as
    ``{identifier}-{phase}.json`` where ``identifier`` is the bare
    issue number when one exists, else the pipeline-id string. We
    mirror that resolution so the handler finds the same file on
    disk — but we deliberately ignore any caller-supplied
    ``pipeline_id`` / ``issue`` so an agent cannot read another
    pipeline's brc-history (path-traversal / cross-pipeline-read
    hardening; risk_analyst R2; reviewer_code NACK #1a).
    """
    env_issue = os.environ.get("EGG_ISSUE_NUMBER")
    if env_issue:
        try:
            return str(int(env_issue))
        except ValueError:
            raise HandlerError(
                f"EGG_ISSUE_NUMBER is set but not an integer: {env_issue!r}"
            ) from None
    pid = get_pipeline_id()
    if pid:
        return str(pid)
    raise HandlerError(
        "pipeline identifier required. "
        "Set EGG_PIPELINE_ID or EGG_ISSUE_NUMBER; caller-supplied values "
        "are rejected for cross-pipeline-read hardening."
    )


def brc_read_peer_artifact(req: dict[str, Any]) -> dict[str, Any]:
    """Read consensus history for a peer from the local brc-history log.

    No CLI counterpart (decision-8): reads from the local
    ``.egg-state/brc-history/`` files written by
    ``orchestrator.routes.pipelines._write_brc_history`` so reviewers
    never have to hand-grep JSON off disk.

    File resolution mirrors the writer's per-slice partition (#2548):

    * ``phase ∈ {refine, plan, pr}`` — reads the aggregate
      ``{identifier}-{phase}.json`` file.
    * ``phase == "implement"`` and ``EGG_SLICE_ID`` is set — reads the
      per-slice ``{identifier}-implement-{slice_id}.json`` file. If a
      sibling ``{identifier}-implement-unattributed.json`` exists
      (cross-cutting messages without slice scope: HEARTBEAT,
      OVERSEER_ALERT, AGENT_FAILED, etc.), its records are merged into
      the response and re-sorted by timestamp so reviewers see the
      slice transcript and the cross-cutting context interleaved.
      Pass ``include_unattributed=False`` to skip the merge.
    * ``phase == "implement"`` and ``EGG_SLICE_ID`` is unset — reads
      the aggregate ``{identifier}-implement.json`` file (babysit_pr
      and other non-slice pipelines).

    Security: caller-supplied ``pipeline_id``/``issue``/``repo_path``
    are ignored; the identifier and repo root are resolved server-side
    from ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` / ``EGG_REPO_PATH``
    (risk_analyst R2 + reviewer_code NACK #1). ``EGG_SLICE_ID`` is
    validated against the canonical ``slice-<N>`` regex before being
    interpolated into the filename — same defense-in-depth as the
    writer-side seam (`orchestrator/routes/pipelines.py` ~line 8406).
    The resolved file path is canonicalised and asserted to sit under
    ``<repo_root>/.egg-state/brc-history/``; anything else raises
    ``HandlerError``. ``peer_role`` must match ``[a-z0-9_-]``.

    Request:
        phase (str): required — one of refine/plan/implement/pr.
        peer_role (str): optional — filter by ``from_role`` on each
            record. Must match ``[a-z0-9_-]``.
        producer_role (str): alias of ``peer_role`` (accepted for
            consistency with existing BRC verbs).
        message_type (str | list[str]): optional filter on
            ``message_type``; accepts a single value or a list.
        limit (int): optional page size (default 50, max 500).
        cursor (str): opaque pagination token.
        include_unattributed (bool): optional, default ``True``. When
            reading a slice-scoped implement transcript, also merge
            records from the sibling
            ``{identifier}-implement-unattributed.json`` file. Set
            ``False`` to read only the per-slice file.

    Response:
        { ok: True, phase: str, items: [...], next_cursor: str|None,
          total_available: int, skipped_malformed: int }

    ``skipped_malformed`` counts brc-history records that were
    silently skipped because they failed isinstance-dict parsing; the
    counter is also embedded in ``next_cursor`` so paginated reads
    remain deterministic.
    """
    phase = req.get("phase")
    if not phase or not isinstance(phase, str):
        raise HandlerError("'phase' is required")
    if phase not in _VALID_PHASES:
        raise HandlerError(f"'phase' must be one of {list(_VALID_PHASES)}; got {phase!r}")

    peer_role = req.get("peer_role") or req.get("producer_role")
    if peer_role is not None:
        if not isinstance(peer_role, str):
            raise HandlerError("'peer_role' must be a string if provided")
        if not _ROLE_SLUG_PATTERN.match(peer_role):
            raise HandlerError(f"'peer_role' must match [a-z0-9_-]; got {peer_role!r}")

    raw_mt = req.get("message_type")
    message_types: frozenset[str] | None
    if raw_mt is None:
        message_types = None
    elif isinstance(raw_mt, str):
        message_types = frozenset({raw_mt})
    elif isinstance(raw_mt, (list, tuple)):
        message_types = frozenset(str(v) for v in raw_mt)
    else:
        raise HandlerError("'message_type' must be a string or list of strings")
    if message_types is not None:
        unknown = message_types - _BRC_HISTORY_TYPES
        if unknown:
            raise HandlerError(
                f"Unknown message_type(s): {sorted(unknown)}; "
                f"expected one of {sorted(_BRC_HISTORY_TYPES)}"
            )

    raw_limit = req.get("limit")
    if raw_limit is None:
        limit = 50
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise HandlerError("'limit' must be an integer") from exc
        if limit <= 0:
            raise HandlerError("'limit' must be > 0")
        if limit > 500:
            raise HandlerError("'limit' must be <= 500")

    raw_include = req.get("include_unattributed")
    if raw_include is None:
        include_unattributed = True
    elif isinstance(raw_include, bool):
        include_unattributed = raw_include
    else:
        raise HandlerError("'include_unattributed' must be a boolean if provided")

    cursor_state = _decode_cursor(req.get("cursor"))
    offset = cursor_state["offset"]
    prior_skipped = cursor_state["skipped_malformed"]

    identifier = _resolve_env_identifier_for_brc_history()
    repo_root = Path(os.environ.get("EGG_REPO_PATH") or os.getcwd()).resolve()
    history_dir = (repo_root / ".egg-state" / "brc-history").resolve()

    # Mirror the writer's per-slice partition for the implement phase
    # (#2548). When EGG_SLICE_ID is set and phase=="implement" we read
    # the slice's transcript file and (by default) merge in the
    # cross-cutting `unattributed` sibling. Other phases and non-slice
    # implement runs read the aggregate file.
    history_files: list[Path] = []
    if phase == "implement":
        slice_id_env = get_slice_id()
        if slice_id_env is not None:
            # Defense-in-depth: validate before interpolating into the
            # filename. The same `^slice-<N>$` regex the orchestrator
            # writer enforces (`pipelines.py` ~8406) so a malformed env
            # value cannot smuggle path separators into the filename.
            if not _SLICE_ID_PATTERN.fullmatch(slice_id_env):
                raise HandlerError(f"Invalid EGG_SLICE_ID {slice_id_env!r}: must match 'slice-<N>'")
            slice_file = (history_dir / f"{identifier}-implement-{slice_id_env}.json").resolve()
            history_files.append(slice_file)
            if include_unattributed:
                unattr_file = (history_dir / f"{identifier}-implement-unattributed.json").resolve()
                history_files.append(unattr_file)
        else:
            history_files.append((history_dir / f"{identifier}-{phase}.json").resolve())
    else:
        history_files.append((history_dir / f"{identifier}-{phase}.json").resolve())

    # Containment check on every resolved path: catches symlinks / .. in
    # identifier/phase/slice_id that escape the allowed directory even
    # after the env-only resolution and SLICE_ID_PATTERN validation.
    for hf in history_files:
        if not hf.is_relative_to(history_dir):
            raise HandlerError("Resolved brc-history path escapes .egg-state/brc-history/")

    records: list[Any] = []
    any_existed = False
    for hf in history_files:
        if not hf.exists():
            continue
        any_existed = True
        try:
            chunk = json.loads(hf.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise HandlerError(
                f"Failed to read brc-history file for phase {phase!r}: {exc}"
            ) from exc
        if not isinstance(chunk, list):
            raise HandlerError(
                f"Malformed brc-history file for phase {phase!r}: expected a JSON array"
            )
        records.extend(chunk)

    if not any_existed:
        return {
            "ok": True,
            "phase": phase,
            "items": [],
            "next_cursor": None,
            "total_available": 0,
            "skipped_malformed": prior_skipped,
        }

    filtered: list[dict[str, Any]] = []
    skipped_malformed = 0
    for rec in records:
        if not isinstance(rec, dict):
            skipped_malformed += 1
            continue
        if peer_role is not None and rec.get("from_role") != peer_role:
            continue
        if message_types is not None and rec.get("message_type") not in message_types:
            continue
        filtered.append(rec)

    # Re-sort merged records by timestamp so the per-slice transcript
    # and the unattributed sibling interleave chronologically. Records
    # without a timestamp sort last, in original order (stable sort).
    if len(history_files) > 1:
        filtered.sort(key=lambda r: (r.get("timestamp") is None, r.get("timestamp") or ""))

    total = len(filtered)
    total_skipped = prior_skipped + skipped_malformed
    if offset >= total:
        page: list[dict[str, Any]] = []
        next_cursor: str | None = None
    else:
        page = filtered[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            _encode_cursor({"offset": next_offset, "skipped_malformed": total_skipped})
            if next_offset < total
            else None
        )

    return {
        "ok": True,
        "phase": phase,
        "items": page,
        "next_cursor": next_cursor,
        "total_available": total,
        "skipped_malformed": total_skipped,
    }
