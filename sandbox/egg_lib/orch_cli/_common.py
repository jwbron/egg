"""Shared CLI helpers: role resolution, handler-error rendering, and prose-arg plumbing (stdin/file channels).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import json
import sys
from typing import Any

from egg_lib import orch_cli as _pkg


def _require_role(args: argparse.Namespace) -> str:
    """Get agent role from args or environment."""
    role = args.role or _pkg.get_agent_role_from_env()
    if not role:
        print("Error: --role required or set EGG_AGENT_ROLE", file=sys.stderr)
        sys.exit(1)
    return role


def _render_handler_error(err: Any) -> int:
    """Render a GatewayError / HandlerError in the legacy orch_cli stderr shape.

    Used by the MCP-counterpart ``cmd_*`` functions so CLI parity is
    preserved when a handler raises instead of returning ``success=False``.
    """
    message = getattr(err, "message", None) or str(err)
    print(f"Error: {message}", file=sys.stderr)
    status = getattr(err, "status_code", None)
    if status:
        print(f"Status: {status}", file=sys.stderr)
    details = getattr(err, "details", None)
    if details:
        try:
            print(f"Details: {json.dumps(details, indent=2)}", file=sys.stderr)
        except TypeError, ValueError:
            pass
    return int(getattr(err, "exit_code", 1))


# ---------------------------------------------------------------------------
# Prose-arg plumbing (#2741, #2908 slice-5)
# ---------------------------------------------------------------------------
#
# Several BRC verbs accept free-form prose arguments — ``--summary``
# (consensus propose), ``--reason`` (consensus ack/nack/withdraw,
# brc resolve-obligation), ``--note`` (brc resolve-obligation). When
# the orchestrator's event-pump wrapper composes a CLI invocation
# from bash, argv-only prose flows through ``bash -c`` and is
# corrupted by shell metacharacters (``$VAR``, backticks, ``;``,
# ``&&``, embedded newlines) — the failure mode #2741 mitigated for
# one verb at a time. Slice-5 generalises the fix: every prose-bearing
# arg now offers a paired ``--FOO-file PATH`` flag and accepts the
# stdin sentinel ``-`` as the argv value. Argv prose still works for
# humans and during transition, but emits a deprecation warning.
#
# ``--files-reviewed-file PATH`` carries an array, one path per line
# per architect v2 §verification_strategy.slice_5. The first delivery
# channel that wins (in order: file, stdin sentinel, argv) provides
# the value; passing two non-empty channels is a hard error so the
# caller can fix its composition site rather than silently dropping
# one channel.


class _ProseArgError(Exception):
    """Raised by the prose-arg helpers on a CLI-level validation failure.

    The cmd_* functions catch this and convert it to a ``return 2``
    (the established pattern for argument-validation failures in
    orch_cli.py — see ``cmd_consensus_ack``'s
    ``--pre-merge-condition-resolved-in-diff`` guard). Going through
    a custom exception (rather than ``sys.exit(2)`` inside the
    helper) keeps the helpers testable in isolation and lets cmd_*
    surface the rc=2 the same way it surfaces other validation
    failures.

    The error message has already been emitted to stderr by the
    helper before the exception is raised; cmd_* only needs to
    convert the exception to ``return 2``.
    """


def _emit_argv_prose_deprecation(arg_name: str, *, suggested_file_flag: str) -> None:
    """Warn that argv prose is corruption-prone; suggest stdin/file form.

    Emitted on stderr exactly once per invocation per offending arg.
    Goes through ``warnings.warn(DeprecationWarning)`` so test harnesses
    can flip ``-W error`` to fail any regression that silently re-adopts
    argv prose under the wrapper bash.
    """
    import warnings

    warnings.warn(
        (
            f"{arg_name}: argv prose flows through ``bash -c`` and is corrupted "
            f"by shell metacharacters (#2741). Prefer {suggested_file_flag} or "
            f"pipe the prose via stdin: ``{arg_name} -``."
        ),
        DeprecationWarning,
        stacklevel=2,
    )


def _resolve_prose_arg(
    *,
    argv_value: str | None,
    file_path: str | None,
    arg_name: str,
    file_flag: str,
    required: bool = True,
) -> str:
    """Resolve a prose argument from argv, stdin sentinel, or file.

    Resolution order — exactly one channel must win:

    1. ``file_path`` is set → read UTF-8 contents of the file. Empty
       file is allowed (the orchestrator may accept an empty reason
       even when one is "required" at the CLI; the handler-layer
       check is the source of truth).
    2. ``argv_value == "-"`` → read UTF-8 contents from stdin.
    3. ``argv_value`` is a non-empty string → use it verbatim, emit
       deprecation warning recommending the file or stdin channel.

    Passing **both** ``argv_value`` (non-empty / non-sentinel) **and**
    ``file_path`` is a hard error — surfaces composition-site bugs
    instead of silently dropping one input. Stdin sentinel ``-`` plus
    ``--FOO-file PATH`` is also rejected for the same reason.
    """
    argv_set = argv_value is not None and argv_value != ""
    file_set = file_path is not None and file_path != ""
    stdin_set = argv_value == "-"

    if file_set and stdin_set:
        print(
            f"Error: {arg_name} - and {file_flag} are mutually exclusive; "
            f"use exactly one delivery channel.",
            file=sys.stderr,
        )
        raise _ProseArgError
    if file_set and argv_set and not stdin_set:
        print(
            f"Error: {arg_name} and {file_flag} are mutually exclusive; "
            f"use exactly one delivery channel.",
            file=sys.stderr,
        )
        raise _ProseArgError

    if file_set:
        assert file_path is not None
        try:
            with open(file_path, encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            # ``UnicodeDecodeError`` is a ``ValueError`` subclass, NOT
            # an ``OSError``, so it slips through a bare ``except
            # OSError`` and lands as a raw traceback on stderr. In the
            # event-pump wrapper context this slice hardens, an
            # operator that points ``--reason-file`` at binary garbage
            # / a BOM-prefixed file / a mixed-encoding diff deserves
            # the same actionable rc=2 + clear stderr message they
            # get for a missing-file path (tester NACK v1).
            print(f"Error: failed to read {file_flag}={file_path}: {exc}", file=sys.stderr)
            raise _ProseArgError from exc

    if stdin_set:
        return sys.stdin.read()

    if argv_set:
        _emit_argv_prose_deprecation(arg_name, suggested_file_flag=file_flag)
        return argv_value  # type: ignore[return-value]

    if required:
        print(
            f"Error: {arg_name} is required (pass {arg_name} VALUE, "
            f"{arg_name} - for stdin, or {file_flag} PATH).",
            file=sys.stderr,
        )
        raise _ProseArgError
    return ""


def _resolve_files_reviewed_arg(
    *,
    argv_value: list[str] | None,
    file_path: str | None,
) -> list[str]:
    """Resolve --files-reviewed (argv list) or --files-reviewed-file (one path per line).

    One-path-per-line semantics per architect v2 §verification_strategy.slice_5.
    Blank lines and lines beginning with ``#`` are stripped (so callers can
    drop comments into a generated review-manifest file). Returns the
    parsed list; passing both channels is a hard error.
    """
    argv_set = argv_value is not None and len(argv_value) > 0
    file_set = file_path is not None and file_path != ""

    if file_set and argv_set:
        print(
            "Error: --files-reviewed and --files-reviewed-file are mutually "
            "exclusive; use exactly one delivery channel.",
            file=sys.stderr,
        )
        raise _ProseArgError

    if file_set:
        assert file_path is not None
        try:
            with open(file_path, encoding="utf-8") as fh:
                raw_lines = fh.read().splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            # See ``_resolve_prose_arg`` for the rationale —
            # ``UnicodeDecodeError`` is a ``ValueError`` subclass and
            # is not caught by a bare ``except OSError``. Treat
            # non-UTF-8 manifests as a clean rc=2 error rather than a
            # raw traceback (tester NACK v1).
            print(
                f"Error: failed to read --files-reviewed-file={file_path}: {exc}",
                file=sys.stderr,
            )
            raise _ProseArgError from exc
        items: list[str] = []
        for line in raw_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            items.append(stripped)
        return items

    if argv_set:
        return list(argv_value or [])
    return []
