"""Overseer subcommands (alert/file-issue/consult-advisor).

Extracted verbatim from the monolithic ``orch_cli.py`` (#3312, slice-17)
per ``docs/guides/decomposition-pattern.md``. Pure refactor — no behaviour
change.
"""

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.parse import quote

from egg_lib import orch_cli as _pkg

from ._common import _render_handler_error
from ._http import (
    _SAFE_ID_PATTERN,
    get_pipeline_id_from_env,
    print_json,
    require_pipeline_id,
)


def cmd_overseer_alert(args: argparse.Namespace) -> int:
    """Broadcast an OVERSEER_ALERT to the human operator.

    Wraps the message-send endpoint with message_type=OVERSEER_ALERT and
    to_role="all" hard-coded so the overseer agent never picks the type by
    hand. The human-facing alert surfaces (sdlc skill, get_status enrichment)
    only react to OVERSEER_ALERT — STATUS/HANDOFF blend into normal traffic.

    Delegates to :func:`egg_agent_tools.handlers.progress.progress_overseer_alert`
    so the CLI and the ``mcp__progress__overseer_alert`` MCP tool share a
    handler (iter-2 drift gate).
    """
    from egg_agent_tools.handlers import progress as _handlers
    from egg_agent_tools.handlers.errors import GatewayError, HandlerError

    pid = require_pipeline_id(args)
    role = args.role or _pkg.get_agent_role_from_env() or "overseer"

    req: dict[str, Any] = {
        "pipeline_id": pid,
        "role": role,
        "anomaly": args.anomaly,
        "priority": args.priority,
        "summary": args.summary,
    }
    if args.detail:
        req["detail"] = args.detail
    if args.recommend:
        req["recommend"] = args.recommend
    # Issue #1962: structured recommendation + payload.
    recommendation = getattr(args, "recommendation", None)
    payload_file = getattr(args, "recommendation_payload_file", None)
    if recommendation:
        if not payload_file:
            print(
                "Error: --recommendation requires --recommendation-payload-file",
                file=sys.stderr,
            )
            return 2
        try:
            with open(payload_file, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"Error: cannot read --recommendation-payload-file: {exc}",
                file=sys.stderr,
            )
            return 2
        req["recommendation"] = recommendation
        req["recommendation_payload"] = payload

    try:
        resp = _handlers.progress_overseer_alert(req)
    except GatewayError as err:
        if args.json:
            print_json({"success": False, "message": err.message or str(err)})
            return int(getattr(err, "exit_code", 1))
        return _render_handler_error(err)
    except HandlerError as err:
        if args.json:
            print_json({"success": False, "message": err.message or str(err)})
            return int(err.exit_code)
        print(f"Error: {err.message}", file=sys.stderr)
        return err.exit_code

    if args.json:
        print_json(resp.get("signal", {}))
        return 0

    msg = resp.get("alert") or {}
    print(f"OVERSEER_ALERT broadcast: {msg.get('id', 'unknown')} ({args.anomaly}, {args.priority})")
    return 0


# ---------------------------------------------------------------------------
# Overseer file-issue (issue #1962, decision-9 opt-1)
# ---------------------------------------------------------------------------

# Hard limits matching the gateway's defense-in-depth checks. Local-side
# rejection means the gateway doesn't have to fail us at the network
# boundary.
_OVERSEER_TITLE_MAX_CHARS = 120
_OVERSEER_BODY_MAX_BYTES = 50_000
_OVERSEER_VALID_LABEL_PRIORITIES = ("p0", "p1", "p2", "p3")


def cmd_overseer_file_issue(args: argparse.Namespace) -> int:
    """File a GitHub issue from the overseer role (issue #1962).

    Runs ``gh issue create`` itself, inside the sandbox, mediated by
    the gateway. There is no orchestrator-side endpoint that runs
    ``gh`` (decision-9 opt-1).

    Reads the issue title and body from local files (no shell-escaping
    headaches). Looks up an existing issue with the same anomaly
    signature first (intra-phase JSONL cache + cross-phase ``gh issue
    list --search`` fallback) and skips the gh call when one is found.
    On a fresh filing, appends a ``FiledIssueRecord`` to
    ``.egg-state/oversight/filed-issues.jsonl`` so a later cycle's
    dedup can short-circuit.

    Output: JSON ``{"issue_number": int, "filed": bool,
    "dedup_match": int|null}``. Exit code 0 on either outcome
    (filed-or-dedup); non-zero only on gh failure or local validation
    failure. The ``--dry-run`` flag prints the composed argv + JSON
    without invoking gh.
    """
    from datetime import UTC, datetime

    from egg_overseer.state import FiledIssueRecord, append_filed_issue

    repo = os.environ.get("EGG_PIPELINE_REPO")
    if not repo:
        print(
            "Error: EGG_PIPELINE_REPO env var is required (set by orchestrator)",
            file=sys.stderr,
        )
        return 2

    # Read title + body files locally so we can validate sizes before
    # the gateway has to. Files are sandbox-local (CLI-supplied paths).
    try:
        with open(args.issue_title_file, encoding="utf-8") as fh:
            title = fh.read().strip()
    except OSError as exc:
        print(f"Error: cannot read --issue-title-file: {exc}", file=sys.stderr)
        return 2
    try:
        with open(args.issue_body_file, "rb") as bfh:
            body_bytes = bfh.read()
    except OSError as exc:
        print(f"Error: cannot read --issue-body-file: {exc}", file=sys.stderr)
        return 2
    # Body itself is passed to gh via --body-file (no need to decode here);
    # the byte length is what we cap on.

    if len(title) > _OVERSEER_TITLE_MAX_CHARS:
        print(
            f"Error: title exceeds {_OVERSEER_TITLE_MAX_CHARS} chars (got {len(title)})",
            file=sys.stderr,
        )
        return 2
    if len(body_bytes) > _OVERSEER_BODY_MAX_BYTES:
        print(
            f"Error: body exceeds {_OVERSEER_BODY_MAX_BYTES} bytes (got {len(body_bytes)})",
            file=sys.stderr,
        )
        return 2

    if args.priority not in _OVERSEER_VALID_LABEL_PRIORITIES:
        # argparse already enforces this via choices; double-check for
        # programmatic callers that bypass the parser.
        print(
            f"Error: --priority must be one of "
            f"{list(_OVERSEER_VALID_LABEL_PRIORITIES)}; got {args.priority!r}",
            file=sys.stderr,
        )
        return 2

    # Dedup pre-check.
    from egg_lib.overseer_issue_body import find_existing_issue

    existing = find_existing_issue(
        repo=repo,
        anomaly_signature=args.anomaly_signature,
    )
    if existing is not None:
        result: dict[str, Any] = {
            "issue_number": existing,
            "filed": False,
            "dedup_match": existing,
        }
        if args.dry_run or args.json:
            print_json(result)
        else:
            print(
                f"Existing issue #{existing} already covers this anomaly; skipping gh issue create."
            )
        # Structured log line for metrics.
        import logging as _logging

        _logging.getLogger("egg_lib.overseer_file_issue").info(
            "overseer_event",
            extra={
                "event": "issue_filed",
                "outcome": "dedup",
                "issue_number": existing,
                "anomaly_signature": args.anomaly_signature,
                "anomaly_type": args.anomaly_type,
                "agent_role": args.agent_role,
            },
        )
        return 0

    # Build the gh argv. We pass --title inline (we've already read and
    # validated the title file locally) because the sandbox `gh` wrapper
    # at sandbox/scripts/gh::handle_issue_create only recognises
    # --title|-t, --body|-b, --body-file|-F, --label|-l, --assignee|-a.
    # --title-file is not a recognised flag and would cause the wrapper
    # to error before reaching the gateway. We also drop --json because
    # the wrapper doesn't pass it through; instead we parse the URL
    # gh prints to stdout to extract the issue number.
    argv = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body-file",
        args.issue_body_file,
        "--label",
        "agent:overseer",
        "--label",
        args.priority,
    ]

    if args.dry_run:
        dry_result: dict[str, Any] = {
            "issue_number": None,
            "filed": False,
            "dedup_match": None,
            "dry_run": True,
            "argv": argv,
            "title": title,
            "body_bytes": len(body_bytes),
        }
        print_json(dry_result)
        return 0

    import subprocess as _subprocess

    try:
        proc = _subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, _subprocess.TimeoutExpired) as exc:
        print(f"Error: gh subprocess failed: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(
            f"Error: gh issue create exited {proc.returncode}: {proc.stderr}",
            file=sys.stderr,
        )
        return 1

    # gh issue create (without --json) prints the issue URL to stdout,
    # one line, e.g. "https://github.com/owner/repo/issues/123\n".
    # The sandbox `gh` wrapper does not pass --json through, so we
    # parse the trailing integer off the URL. Keep this resilient to
    # minor whitespace and trailing-slash variations.
    issue_number: int | None = None
    raw_stdout = (proc.stdout or "").strip()
    # First try JSON (covers tests / future wrapper extensions that
    # surface the --json output).
    if raw_stdout.startswith("{"):
        try:
            gh_payload = json.loads(raw_stdout)
            num = gh_payload.get("number")
            if isinstance(num, int):
                issue_number = num
        except json.JSONDecodeError:
            issue_number = None
    if issue_number is None:
        match = re.search(r"/issues/(\d+)", raw_stdout)
        if match:
            try:
                issue_number = int(match.group(1))
            except ValueError:
                issue_number = None
    if issue_number is None:
        print(
            f"Error: gh stdout did not contain an issue number: {raw_stdout!r}",
            file=sys.stderr,
        )
        return 1

    # Persist to JSONL cache for intra-phase dedup.
    try:
        append_filed_issue(
            ".egg-state/oversight/filed-issues.jsonl",
            FiledIssueRecord(
                issue_number=issue_number,
                anomaly_type=args.anomaly_type,
                anomaly_signature=args.anomaly_signature,
                agent_role=args.agent_role,
                repo=repo,
                pipeline_id=os.environ.get("EGG_PIPELINE_ID", ""),
                phase=os.environ.get("EGG_PHASE", ""),
                filed_at=datetime.now(UTC),
                parent_alert_message_id=getattr(args, "parent_alert_message_id", None),
                hitl_outcome="filed",
            ),
        )
    except OSError as exc:
        # Filing succeeded; cache write failure is loggable but not fatal.
        import logging as _logging

        _logging.getLogger("egg_lib.overseer_file_issue").warning(
            "overseer_event",
            extra={
                "event": "issue_filed_cache_failed",
                "issue_number": issue_number,
                "error": str(exc),
            },
        )

    import logging as _logging

    _logging.getLogger("egg_lib.overseer_file_issue").info(
        "overseer_event",
        extra={
            "event": "issue_filed",
            "outcome": "filed",
            "issue_number": issue_number,
            "anomaly_signature": args.anomaly_signature,
            "anomaly_type": args.anomaly_type,
            "agent_role": args.agent_role,
        },
    )

    filed_result: dict[str, Any] = {
        "issue_number": issue_number,
        "filed": True,
        "dedup_match": None,
    }
    if args.json:
        print_json(filed_result)
    else:
        print(f"Filed issue #{issue_number} ({args.anomaly_type}, {args.priority})")
    return 0


def cmd_overseer_consult_advisor(args: argparse.Namespace) -> int:
    """Consult the Opus advisor for a structured verdict (issue #1962).

    Runs ``egg_overseer.advisor.consult_advisor`` itself, inside the
    sandbox, so the underlying ``run_agent_async`` call lives on the
    LLM-execution side of the EGG200 boundary (``docs/guides/agent-mode-design.md``)
    and the orchestrator pod never touches Anthropic credentials.

    Reads the inputs (Haiku classification + Tier-1 health alerts +
    optional progress events / log lines) from a JSON file. Writes the
    validated ``AdvisorVerdict`` JSON to ``--output-file`` (or stdout
    when omitted). The caller (the overseer agent) is expected to gate
    the call behind ``should_consult_advisor`` (Haiku confidence ≥ 0.8
    AND a Tier-1 health alert active).

    Output (JSON): the verdict dict from ``AdvisorVerdict.model_dump()``.
    Exit codes:
        0 — success
        1 — advisor parse failure (the SDK returned a payload that did
            not match the ``AdvisorVerdict`` schema; the caller should
            classify as a parse drift, not a transient failure)
        2 — input validation / I/O failure (missing or malformed
            ``--inputs-file``; unwritable ``--output-file``)
        3 — advisor runtime failure (network, auth, rate-limit, or any
            other unhandled exception from the SDK call); distinct from
            parse failure so the caller can back off / retry vs. treat
            as a model-output drift
    """
    import asyncio
    from types import SimpleNamespace

    from egg_overseer.advisor import AdvisorParseError, consult_advisor

    inputs_path = args.inputs_file
    try:
        with open(inputs_path, encoding="utf-8") as fh:
            inputs = json.load(fh)
    except OSError as exc:
        print(f"Error: cannot read --inputs-file: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"Error: --inputs-file is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(inputs, dict):
        print("Error: --inputs-file must be a JSON object", file=sys.stderr)
        return 2

    classification = inputs.get("classification") or {}
    health_alerts = inputs.get("health_alerts") or []
    progress_events = inputs.get("progress_events") or []
    recent_log_lines = inputs.get("recent_log_lines") or []

    if not isinstance(classification, dict):
        print("Error: inputs.classification must be an object", file=sys.stderr)
        return 2
    if not isinstance(health_alerts, list):
        print("Error: inputs.health_alerts must be an array", file=sys.stderr)
        return 2
    if not isinstance(progress_events, list):
        print("Error: inputs.progress_events must be an array", file=sys.stderr)
        return 2
    if not isinstance(recent_log_lines, list):
        print("Error: inputs.recent_log_lines must be an array", file=sys.stderr)
        return 2

    # Resolve advisor config knobs from PipelineConfig when a pipeline-id
    # is available (issues #2113, #2170). The orchestrator's status
    # endpoint exposes the overseer-relevant config subset
    # (orchestrator/routes/pipelines.py); we read each field and pass a
    # duck-typed config to consult_advisor. Falling back to config=None
    # keeps the historic defaults ("opus" model, 256 KiB log cap) for
    # callers that do not provide a pipeline-id, and for any failure
    # (orchestrator unreachable, malformed env, missing client module) —
    # never crash the verb on the lookup path. NOTE: extend the
    # SimpleNamespace assembly below if consult_advisor ever reads more
    # `config.*` attributes; the duck-typed surface silently falls back
    # to AttributeError today.
    advisor_config: Any = None
    pid = getattr(args, "pipeline_id", None) or get_pipeline_id_from_env()
    if pid and _SAFE_ID_PATTERN.match(pid):
        # Nested try so ImportError is handled before OrchestratorError is
        # referenced — combining them in a single except clause raises
        # NameError when the import itself fails (OrchestratorError is
        # never bound). See review feedback on PR #2158.
        try:
            from egg_lib.orch_client import OrchClient, OrchestratorError
        except ImportError as exc:
            print(
                f"Warning: cannot import egg_lib.orch_client ({exc}); "
                f"falling back to default advisor config",
                file=sys.stderr,
            )
        else:
            try:
                status = OrchClient().get_pipeline_status(quote(pid, safe=""))
                cfg_dict = status.get("config") if isinstance(status, dict) else None
                if isinstance(cfg_dict, dict):
                    ns_kwargs: dict[str, Any] = {}
                    model = cfg_dict.get("overseer_advisor_model")
                    if model:
                        ns_kwargs["overseer_advisor_model"] = model
                    # bytes-cap can legitimately be 0 (disable sentinel),
                    # so distinguish "absent" from "explicitly zero" with
                    # `is not None` rather than truthiness.
                    cap = cfg_dict.get("overseer_advisor_recent_log_bytes_cap")
                    if cap is not None:
                        ns_kwargs["overseer_advisor_recent_log_bytes_cap"] = cap
                    if ns_kwargs:
                        advisor_config = SimpleNamespace(**ns_kwargs)
            except OrchestratorError as exc:
                print(
                    f"Warning: cannot read PipelineConfig for {pid} "
                    f"({exc}); falling back to default advisor config",
                    file=sys.stderr,
                )
    elif pid:
        # Malformed pipeline-id (e.g. corrupted EGG_PIPELINE_ID): skip
        # the lookup silently rather than crashing via validate_id's
        # sys.exit(1), which would collide with AdvisorParseError's
        # exit-code semantics. See review feedback on PR #2158.
        print(
            f"Warning: pipeline_id {pid!r} is not a safe ID; falling back to default advisor config",
            file=sys.stderr,
        )

    recent_log_bytes_cap = getattr(args, "recent_log_bytes_cap", None)
    try:
        verdict = asyncio.run(
            consult_advisor(
                classification=classification,
                health_alerts=health_alerts,
                progress_events=progress_events,
                recent_log_lines=recent_log_lines,
                config=advisor_config,
                recent_log_bytes_cap=recent_log_bytes_cap,
            )
        )
    except AdvisorParseError as exc:
        print(f"Error: advisor parse failure: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Distinct exit code for SDK / runtime failures (network, auth,
        # rate-limit) so the caller can distinguish them from a parse
        # drift on AdvisorVerdict. The overseer agent uses this to
        # decide between retry / back-off and re-classifying the model
        # output.
        print(
            f"Error: advisor runtime failure ({type(exc).__name__}): {exc}",
            file=sys.stderr,
        )
        return 3

    payload = verdict.model_dump()
    rendered = json.dumps(payload, indent=2, sort_keys=True)

    if args.output_file:
        try:
            with open(args.output_file, "w", encoding="utf-8") as fh:
                fh.write(rendered)
        except OSError as exc:
            print(f"Error: cannot write --output-file: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print_json(payload)
        else:
            print(f"Wrote AdvisorVerdict to {args.output_file}")
    else:
        # No --output-file: stdout is the only sink, so the verdict
        # JSON always lands there. ``--json`` is meaningful only with
        # ``--output-file`` (where it controls whether to tee the
        # verdict to stdout in addition to writing the file).
        print(rendered)
    return 0
