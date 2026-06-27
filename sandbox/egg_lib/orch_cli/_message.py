"""Inter-agent message subcommands (send/poll/wait/wait-loop/heartbeat/status) plus wait-cursor file helpers.

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import os
import sys
from typing import Any
from urllib.parse import urlencode

from egg_lib import orch_cli as _pkg

from ._http import (
    _SAFE_ID_PATTERN,
    print_json,
    resolve_slice_id,
)


def cmd_message_send(args: argparse.Namespace) -> int:
    """Send an inter-agent message."""
    pid = _pkg.require_pipeline_id(args)
    role = args.role or _pkg.get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        sys.exit(1)

    data: dict[str, Any] = {
        "from_role": role,
        "to_role": args.to,
        "message_type": args.type,
        "subject": args.subject or "",
        "body": args.body or "",
    }

    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/messages", method="POST", data=data)

    if args.json:
        print_json(result)
        return 0

    if result.get("success"):
        msg = result.get("data", {}).get("message", {})
        print(f"Message sent: {msg.get('id', 'unknown')}")
        return 0
    print(f"Error: {result.get('message')}", file=sys.stderr)
    return 1


def cmd_message_poll(args: argparse.Namespace) -> int:
    """Poll for inter-agent messages."""
    pid = _pkg.require_pipeline_id(args)

    params: dict[str, str] = {}
    role = args.role or _pkg.get_agent_role_from_env()
    if role:
        params["role"] = role
    if args.since:
        params["since_id"] = args.since
    if args.limit:
        params["limit"] = str(args.limit)
    wait = getattr(args, "wait", None)
    if wait is not None:
        params["wait"] = str(wait)

    endpoint = f"/api/v1/pipelines/{pid}/messages"
    if params:
        endpoint += "?" + urlencode(params)

    # Use a longer timeout when long-polling to avoid client-side timeout
    timeout = (wait + 5) if wait else 15
    result = _pkg.orch_request(endpoint, timeout=timeout)

    if args.json:
        print_json(result)
        return 0

    messages = result.get("data", {}).get("messages", [])
    if not messages:
        print("No messages.")
        return 0

    for msg in messages:
        ts = msg.get("timestamp", "")[:19]
        from_r = msg.get("from_role", "?")
        to_r = msg.get("to_role", "?")
        mtype = msg.get("message_type", "?")
        subject = msg.get("subject", "")
        print(f"  [{ts}] {from_r} -> {to_r} ({mtype}): {subject}")
        body = msg.get("body", "")
        if body:
            # Indent multi-line bodies for readability
            indented = body.replace("\n", "\n    ")
            print(f"    {indented}")

    print(f"\n{len(messages)} message(s)")
    return 0


def _classify_gateway_error_rc(status: int | None) -> int:
    """Map a GatewayError status onto message-wait's transient/permanent rc."""
    if status is not None and 400 <= status < 500 and status != 408:
        return 3
    # 5xx, 408, connection / timeout failures
    return 2


def _wait_cursor_path(
    pipeline_id: str | None,
    role: str | None,
    for_types: list[str],
    from_role: str | None = None,
    from_roles: list[str] | None = None,
    slice_id: str | None = None,
) -> str | None:
    """Derive the cursor file path for a wait call (issue #2323).

    The cursor file is the file-system back-channel that threads the
    response cursor across successive ``wait`` / ``wait-loop`` CLI
    invocations. Without it, every re-entered wait restarts at the
    stream tip and silently misses any event that landed in the gap
    between calls — the multi-producer reviewer stall #2323 documents.

    Path scheme:
    ``{EGG_WAIT_CURSOR_DIR}/egg-wait-cursor-{pipeline_id}-{role}-{hash}``
    where ``hash`` is an MD5 of the **sorted** ``for_types`` together
    with the ``from_role`` filter (if any). Sorting makes order-permuted
    callers share a file; including ``from_role`` keeps two waits with
    the same ``for`` set but different sender filters from sharing a
    cursor — a wait advancing past a message its ``--from`` filter
    dropped would otherwise cause a sibling wait with a different
    filter to miss it. Including ``pipeline_id`` keeps cursors from
    bleeding across pipelines that happen to reuse the same container
    or ``/tmp`` mount (debug shells, integration test reuse).

    Returns ``None`` when no role is available — debug shells without
    ``EGG_AGENT_ROLE`` set get the legacy from-tip behavior with no
    file-system side effects, since there's no obvious agent identity
    to scope a cursor to. Also returns ``None`` when ``role`` or
    ``pipeline_id`` contain characters outside the safe-ID alphabet
    (``[a-zA-Z0-9_\\-.]``). The load-bearing rejection is path
    separators (``/``); ``.`` and ``-`` are inside the alphabet, so
    a literal ``..`` or ``.`` substring is *permitted* within a single
    filename component — which is harmless because the path is one
    component and there's no traversal target. ``cmd_message_wait`` /
    ``cmd_message_wait_loop`` already pass ``pipeline_id`` through
    ``validate_id`` before reaching here, so this check is symmetric
    defense-in-depth for ``role`` (which is taken straight from
    ``EGG_AGENT_ROLE``). Tests override ``EGG_WAIT_CURSOR_DIR`` to
    redirect cursor writes off ``/tmp``.
    """
    if not role or not for_types:
        return None
    if not _SAFE_ID_PATTERN.match(role):
        return None
    if pipeline_id is not None and not _SAFE_ID_PATTERN.match(pipeline_id):
        return None
    import hashlib

    base = os.environ.get("EGG_WAIT_CURSOR_DIR", "/tmp")
    types_key = ",".join(sorted(for_types))
    # #2725: include the new filter axes in the hash so a scoped wait
    # never shares a cursor with an unscoped sibling. Two waits whose
    # filters could diverge on which messages they keep MUST have
    # different cursor files — otherwise a wait that advanced its
    # cursor past a message its filter dropped would cause the sibling
    # to silently miss that message.
    from_roles_key = ",".join(sorted(from_roles)) if from_roles else ""
    hash_input = (
        f"{types_key}|from={from_role or ''}|from_set={from_roles_key}|slice={slice_id or ''}"
    )
    digest = hashlib.md5(hash_input.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    pid_segment = pipeline_id or "no-pipeline"
    return os.path.join(base, f"egg-wait-cursor-{pid_segment}-{role}-{digest}")


def _read_cursor_file(path: str | None) -> str | None:
    """Read a wait cursor from *path*.

    Companion to :func:`_wait_cursor_path`. Returns ``None`` if the
    path is unset, the file is missing, or the file is empty — all
    three behave identically to "no ``--since`` supplied", so the
    server's default from-tip semantics apply on the very first call.

    Read failures are logged to stderr but never raised: a wonky
    filesystem on the cursor path must not turn a recoverable wait
    into a hard failure.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        # ``ValueError`` covers ``UnicodeDecodeError`` (its subclass) — a
        # non-UTF-8 cursor file (corrupted, manually edited with bad
        # bytes, bind-mount confusion) must not crash a wait that
        # otherwise would have succeeded.
        print(f"Warning: could not read cursor file {path}: {err}", file=sys.stderr)
        return None
    return value or None


def _delete_cursor_file(path: str | None) -> None:
    """Remove a stale cursor file (issue #2464).

    Called when the server signals ``since_id_stale: true`` — i.e. the
    cached cursor pointed at a message the store no longer indexes
    (typically because a phase-boundary ``clear`` wiped it). Dropping
    the file causes the next wait to re-snap to tip instead of feeding
    the dead cursor back forever.

    Best-effort: a missing file is the desired post-state and any other
    OSError just leaves the file in place — the next call will redo the
    full-history fallback once and write a fresh cursor on success,
    which clears the staleness on its own.
    """
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as err:  # pragma: no cover - filesystem-dependent
        print(f"Warning: could not unlink stale cursor file {path}: {err}", file=sys.stderr)


def _write_cursor_file(path: str | None, cursor: str | None) -> None:
    """Atomically persist *cursor* under *path*.

    Writes via tmp-in-same-dir + ``os.replace`` so a partially-written
    cursor file is never observable. A ``None`` / empty cursor is a
    no-op — preserving any prior cursor on disk. This is reachable
    when the message route returns ``cursor=null`` (an empty stream
    on the very first wait of a fresh pipeline) and when a pathological
    safety-cap exit produces an empty response: the invariant
    "the cursor file never moves backward" holds unconditionally.

    Best-effort: write failures log a warning but do not affect the
    caller's exit code. The wait already succeeded; a missed cursor
    persist just reopens the same race the file was added to close,
    which the caller will notice on the next missed event.
    """
    if not path:
        return
    if not isinstance(cursor, str) or not cursor.strip():
        # Empty / non-string cursor → leave any prior cursor on disk
        # alone. The wire contract says ``cursor`` is ``str | None``,
        # but a future contract weakening (e.g., int message ID) would
        # otherwise raise ``AttributeError`` on ``.strip()`` mid-write
        # and surface as a stack trace after the wait already returned
        # results to the caller.
        return
    parent = os.path.dirname(path) or "."
    tmp_path = f"{path}.tmp.{os.getpid()}"
    fd: int | None = None
    try:
        os.makedirs(parent, exist_ok=True)
        # ``O_NOFOLLOW`` rejects a symlink at ``tmp_path``: a stale
        # dangling symlink left behind by an interrupted prior write
        # must not redirect this one. ``O_EXCL`` makes the create
        # atomic; ``0o600`` keeps the cursor private to the agent.
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None  # ownership transferred to fh
            fh.write(cursor.strip())
        os.replace(tmp_path, path)
    except OSError as err:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # Tidy up a half-written tmp file so a later O_EXCL retry can
        # succeed; ignore failures here too — best-effort hygiene.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        print(f"Warning: could not write cursor file {path}: {err}", file=sys.stderr)


def _resolve_from_producer_arg(cli_values: list[str] | None) -> list[str]:
    """Resolve the producer allowlist from CLI args and the env var (#2725).

    Explicit CLI ``--from-producer`` args (repeatable) replace the
    env-derived default. Each CLI value is split on commas so
    ``--from-producer coder,tester`` and ``--from-producer coder
    --from-producer tester`` behave identically; without the split
    the value would be treated as a single literal role
    ``"coder,tester"`` that matches no real sender, silently
    sleeping the wait through every event — exactly the failure
    mode the wake-storm filter exists to avoid (see #2727 review).

    Falls back to ``$EGG_WAIT_PRODUCER_ALLOWLIST`` (the spawner-set
    env var) when no CLI value was supplied. Empty values are
    skipped on both paths.
    """
    cli_list = list(cli_values or [])
    if cli_list:
        out: list[str] = []
        for value in cli_list:
            out.extend(r.strip() for r in value.split(",") if r.strip())
        return out
    env_allowlist = os.environ.get("EGG_WAIT_PRODUCER_ALLOWLIST", "")
    return [r.strip() for r in env_allowlist.split(",") if r.strip()]


def cmd_message_wait(args: argparse.Namespace) -> int:
    """Event-driven wait for a message of one or more types.

    Issue #1897: the canonical blocking primitive for BRC coordination.
    Agents should prefer this over ``message poll --wait`` with shell-level
    retry loops.

    Delegates to :func:`egg_agent_tools.handlers.message.message_wait`.

    Exit codes (contract):
        0 — one or more matching messages returned (printed to stdout).
        1 — timeout elapsed with no match.
        2 — transient error (5xx, network hiccup, JSON parse failure).
            Retrying is safe.
        3 — permanent error (4xx other than 408, bad pipeline id,
            argparse/config failure). Retrying will not help.
    """
    from egg_agent_tools.handlers import message as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    try:
        pid = _pkg.require_pipeline_id(args)
    except SystemExit:
        return 3
    # require_pipeline_id validates but exits(1) — wrap semantics into 3.

    role = args.role or _pkg.get_agent_role_from_env()
    for_types_list = list(args.for_ or [])
    from_role = getattr(args, "from_", None)
    # #2725: slice scope + producer allowlist with env-var defaults.
    # Spawner sets EGG_SLICE_ID + EGG_WAIT_PRODUCER_ALLOWLIST so the
    # canonical wait idiom auto-scopes without rubric changes. Explicit
    # CLI args take precedence over env defaults.
    # ``resolve_slice_id`` validates ``$EGG_SLICE_ID`` against the
    # canonical ``slice-<N>`` shape and exits 1 on a malformed value,
    # so a misconfigured env var fails fast at the agent instead of
    # producing a tight 400-error retry loop against the route.
    slice_id_arg = getattr(args, "slice_id", None) or resolve_slice_id()
    from_producer_arg = _resolve_from_producer_arg(getattr(args, "from_producer", None))
    # Cursor file is auto-derived per (pipeline_id, role, for_types,
    # from_role, from_roles, slice_id) so every wait re-entry threads
    # its cursor without callers having to opt in (issue #2323). The
    # extra axes (#2725) keep a scoped wait from sharing a cursor with
    # an unscoped sibling. Explicit --since still wins, for callers
    # that want to resume from a specific anchor; ``role`` unset
    # (debug shells) skips cursor handling entirely.
    cursor_file = _wait_cursor_path(
        pid,
        role,
        for_types_list,
        from_role,
        from_roles=from_producer_arg or None,
        slice_id=slice_id_arg,
    )
    effective_since = args.since or _read_cursor_file(cursor_file)
    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "for_types": for_types_list,
        "timeout": args.timeout if args.timeout is not None else 60,
    }
    if from_role:
        req["from_role"] = from_role
    elif from_producer_arg:
        req["from_roles"] = from_producer_arg
    if slice_id_arg:
        req["slice_id"] = slice_id_arg
    if effective_since:
        req["since"] = effective_since
    if args.limit:
        req["limit"] = args.limit

    try:
        resp = _handlers.message_wait(req)
    except GatewayError as err:
        # GatewayError subclasses HandlerError — match it first so
        # transient (5xx/408/network) failures map to rc=2, not rc=3.
        rc = _classify_gateway_error_rc(err.status_code)
        prefix = "Error" if rc == 3 else "Transient error"
        print(f"{prefix}: {err.message}", file=sys.stderr)
        return rc
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 3
    except Exception as err:  # pragma: no cover - defensive
        print(f"Unexpected error: {err}", file=sys.stderr)
        return 2

    messages = list(resp.get("messages") or [])
    matched = bool(resp.get("matched"))

    # Issue #2464: when the server flagged the request's ``since`` as
    # stale, drop the cached cursor file before deciding whether to
    # write a new one. The fresh cursor below replaces it on the
    # common path (server returns the new tip on every response); the
    # explicit unlink covers the rare case where the server returns a
    # null cursor on the same response (empty stream after clear) so
    # the dead value doesn't survive into the next wait.
    if resp.get("since_id_stale"):
        _delete_cursor_file(cursor_file)
    # Persist the response cursor on every successful round-trip
    # (match OR timeout). The server returns the latest stream tip on
    # timeout so the next call resumes strictly after what this one
    # would have seen; on match it returns the ID of the last
    # delivered message. Either way, threading closes the wait→wait
    # race that motivated #2323.
    _write_cursor_file(cursor_file, resp.get("cursor"))

    if args.json:
        print_json(resp.get("raw", {}))
    else:
        if matched:
            for msg in messages:
                ts = msg.get("timestamp", "")[:19]
                from_r = msg.get("from_role", "?")
                to_r = msg.get("to_role", "?")
                mtype = msg.get("message_type", "?")
                subject = msg.get("subject", "")
                print(f"  [{ts}] {from_r} -> {to_r} ({mtype}): {subject}")
                body = msg.get("body", "")
                if body:
                    indented = body.replace("\n", "\n    ")
                    print(f"    {indented}")
            print(f"\n{len(messages)} message(s) matched")

    return 0 if matched else 1


def cmd_message_wait_loop(args: argparse.Namespace) -> int:
    """Wrapper-internal wait-loop convenience command (issue #1897).

    Called by the consensus wrapper (the event pump, #2908) between
    actionable BRC events — never by agents, which are invoked one-shot
    per event and must exit instead of waiting (#3157; see
    docs/reference/agent-wait-patterns.md §0).

    Loops **forever** until a matching message arrives (exit 0, prints
    the message) OR a permanent error occurs (exit 1).  The missing
    outer timeout is intentional — BRC consensus can legitimately take
    hours on long phases, and a caller wrapping this in its own outer
    loop would defeat the purpose.

    Exit codes (wrapper contract, DIFFERENT from ``message wait``):

      * 0 — a matching message arrived; it is printed to stdout.
      * 1 — a permanent error occurred (bad pipeline id, auth, argparse
        misuse propagated from an inner ``message wait`` rc=3).
        Callers should NOT retry.

    Transient errors (rc=2 from the inner call) are retried with short
    exponential backoff (cap 5s).  Timeouts (rc=1) re-enter the loop
    with a fresh inner call so the caller keeps blocking on the next
    event.

    ``--max-iterations`` is a safety valve only — its default is
    effectively unbounded (``sys.maxsize``) so normal BRC consensus
    never trips it.  The CLI help advertises it as "loops forever by
    default".

    Delegates to
    :func:`egg_agent_tools.handlers.message.message_wait_loop`.
    """
    from egg_agent_tools.handlers import message as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    # --json is not supported on wait-loop (produces concatenated JSON
    # objects on stdout across iterations).  Force it off so downstream
    # renders match the legacy behaviour.
    args.json = False

    try:
        pid = _pkg.require_pipeline_id(args)
    except SystemExit:
        return 1

    role = args.role or _pkg.get_agent_role_from_env()
    for_types_list = list(args.for_ or [])
    from_role = getattr(args, "from_", None)
    # #2725: slice scope + producer allowlist with env-var defaults —
    # mirrors cmd_message_wait. The auto-apply makes the wake-storm fix
    # transparent: spawner-set env vars suffice; rubrics don't change.
    # ``resolve_slice_id`` validates ``$EGG_SLICE_ID`` and exits 1 on
    # a malformed value (see cmd_message_wait for rationale).
    slice_id_arg = getattr(args, "slice_id", None) or resolve_slice_id()
    from_producer_arg = _resolve_from_producer_arg(getattr(args, "from_producer", None))
    # Cursor file is auto-derived per (pipeline_id, role, for_types,
    # from_role, from_roles, slice_id) — see cmd_message_wait for the
    # rationale (issues #2323 + #2725).
    cursor_file = _wait_cursor_path(
        pid,
        role,
        for_types_list,
        from_role,
        from_roles=from_producer_arg or None,
        slice_id=slice_id_arg,
    )
    effective_since = args.since or _read_cursor_file(cursor_file)
    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "for_types": for_types_list,
        "timeout": args.timeout if args.timeout is not None else 60,
    }
    if from_role:
        req["from_role"] = from_role
    elif from_producer_arg:
        req["from_roles"] = from_producer_arg
    if slice_id_arg:
        req["slice_id"] = slice_id_arg
    if effective_since:
        req["since"] = effective_since
    if args.limit:
        req["limit"] = args.limit
    if args.max_iterations is not None and args.max_iterations > 0:
        req["max_iterations"] = args.max_iterations

    try:
        resp = _handlers.message_wait_loop(req)
    except GatewayError as err:
        # GatewayError subclasses HandlerError — match it first.  The
        # handler reclassifies 4xx (non-408) as permanent and re-raises;
        # the wait-loop contract collapses both GatewayError and
        # HandlerError to rc=1 so callers can't confuse them with
        # transient misses. Cursor file is left alone — the wait did
        # not advance, so the next call should resume from the same
        # place.
        print(f"Error: {err.message}", file=sys.stderr)
        return 1
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 1

    # Issue #2464: drop the cached cursor before re-writing if the
    # final inner iteration's response flagged staleness. The handler
    # already drops ``inner["since"]`` mid-loop when it sees the flag,
    # but we still need to make sure a later call doesn't re-read the
    # dead cursor off disk if the server happened to return a null
    # tip on the staleness response.
    if resp.get("since_id_stale"):
        _delete_cursor_file(cursor_file)
    # Persist the response cursor whenever we have one (match OR
    # safety-cap exit). The handler threads cursors internally between
    # inner iterations, so ``resp["cursor"]`` is the latest tip the
    # loop observed. Writing it on safety-cap means a follow-up
    # invocation skips events the loop has already filtered past
    # rather than rescanning from the original tip.
    _write_cursor_file(cursor_file, resp.get("cursor"))

    messages = list(resp.get("messages") or [])
    matched = bool(resp.get("matched"))
    if matched:
        for msg in messages:
            ts = msg.get("timestamp", "")[:19]
            from_r = msg.get("from_role", "?")
            to_r = msg.get("to_role", "?")
            mtype = msg.get("message_type", "?")
            subject = msg.get("subject", "")
            print(f"  [{ts}] {from_r} -> {to_r} ({mtype}): {subject}")
            body = msg.get("body", "")
            if body:
                indented = body.replace("\n", "\n    ")
                print(f"    {indented}")
        print(f"\n{len(messages)} message(s) matched")
        return 0
    # Safety cap tripped — extraordinarily unlikely with the default
    # sys.maxsize cap. Return 1 (no match) so callers behave the same
    # as a bounded-retry timeout.
    return 1


def cmd_message_heartbeat(args: argparse.Namespace) -> int:
    """Emit a structured HEARTBEAT message (issue #1897).

    Delegates to
    :func:`egg_agent_tools.handlers.message.message_heartbeat`.  The
    handler POSTs to the dedicated ``/api/v1/pipelines/{id}/heartbeat``
    endpoint which handles schema validation, per-role dedup, and the
    ``EGG_HEARTBEAT_RATE_LIMIT`` 429 response.  HTTP 429 is treated as a
    rate-limit error (exit 3 per the CLI contract — caller should honour
    the server's suggested ``retry_after``).
    """
    from egg_agent_tools.handlers import message as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    try:
        pid = _pkg.require_pipeline_id(args)
    except SystemExit:
        return 3

    role = args.role or _pkg.get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        return 3

    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "state": args.state,
    }
    if args.waiting_on:
        req["waiting_on"] = args.waiting_on
    if args.since:
        req["since"] = args.since
    if args.body:
        req["body"] = args.body

    try:
        resp = _handlers.message_heartbeat(req)
    except GatewayError as err:
        # GatewayError subclasses HandlerError — match it first so we
        # can distinguish transport failures (rc=2/3 by status) from
        # user input errors (rc=3).
        # 429 rate-limit is a permanent error from this invocation's
        # perspective — caller should honour retry_after and try again
        # later.
        if err.status_code == 429:
            retry_after = 60
            try:
                if err.details and isinstance(err.details, dict):
                    retry_after = int(err.details.get("retry_after", 60))
            except TypeError, ValueError:
                pass
            print(
                f"Error: HEARTBEAT rate limit exceeded; retry after {retry_after}s.",
                file=sys.stderr,
            )
            return 3
        rc = _classify_gateway_error_rc(err.status_code)
        prefix = "Error" if rc == 3 else "Transient error"
        print(f"{prefix}: {err.message}", file=sys.stderr)
        return rc
    except HandlerError as err:
        print(f"Error: {err.message}", file=sys.stderr)
        return 3

    if args.json:
        print_json(resp.get("signal", {}))
        return 0
    if resp.get("deduped"):
        print(f"HEARTBEAT deduped (unchanged state {args.state})")
    else:
        print(f"HEARTBEAT sent: {args.state}")
    return 0


def cmd_message_status(args: argparse.Namespace) -> int:
    """Get message bus status."""
    pid = _pkg.require_pipeline_id(args)
    result = _pkg.orch_request(f"/api/v1/pipelines/{pid}/messages/status")

    if args.json:
        print_json(result)
        return 0

    data = result.get("data", result)
    print(f"Total messages: {data.get('total', 0)}")
    by_type = data.get("by_type", {})
    if by_type:
        for mtype, count in by_type.items():
            print(f"  {mtype}: {count}")
    return 0


# ---------------------------------------------------------------------------
# Overseer commands (escalation surface)
# ---------------------------------------------------------------------------
