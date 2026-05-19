#!/usr/bin/env python3
"""Write the operator's answer into ``pending_hitl`` atomically.

This helper replaces the inline ``python3 -c "..."`` write that the
skill loop documented in earlier slices of #2717. Doing this in a
dedicated script lets the skill's ``allowed-tools`` scope ``python3``
to the ``bin/*`` directory (so a prompt-injected issue body cannot
coerce the skill into running arbitrary Python) and makes the load-
bearing answer-write read-reviewable next to ``run_pipeline.py``.

The helper reads the operator's answer as a JSON-encoded string from
stdin (default) or from ``--answer-json``, then:

1. Loads ``.egg-state/contracts/<pipeline-id>.json`` (raising loudly
   on parse errors rather than silently overwriting with a default
   skeleton — losing ``answer_log`` would silently re-prompt the
   operator).
2. Decodes the JSON answer (so shell quoting can never mis-encode an
   answer string like ``approve`` into a Python ``NameError``).
3. Sets ``pending_hitl.answer`` and ``pending_hitl.status = "answered"``.
4. Refreshes ``pending_hitl.timestamp`` to ``datetime.now(UTC).isoformat()``
   (no trailing ``Z`` — matches ``run_pipeline.py``'s ``_now_iso`` so
   the two surfaces never drift).
5. Writes the contract atomically via tmp + ``os.replace`` (mirrors
   ``run_pipeline.py``'s ``_write_contract`` so the file is never
   observed half-written).

Exit codes:

* ``0`` — answer written, contract flushed atomically.
* ``1`` — argument / JSON-decode error, missing contract, or
  unwritable target. Diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string.

    Matches ``run_pipeline.py:_now_iso`` so the two writers produce
    identical timestamp formats.
    """
    return datetime.now(UTC).isoformat()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="write_answer.py",
        description=(
            "Write the operator's answer into the pending_hitl envelope "
            "atomically. Companion to run_pipeline.py."
        ),
    )
    parser.add_argument(
        "--pipeline-id",
        required=True,
        help="Pipeline identifier (e.g. 'issue-1234').",
    )
    parser.add_argument(
        "--state-root",
        default=None,
        help="Override the .egg-state/ root (defaults to <cwd>/.egg-state).",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--answer-stdin",
        action="store_true",
        help="Read the JSON-encoded answer from stdin.",
    )
    src.add_argument(
        "--answer-json",
        default=None,
        help="JSON-encoded answer literal (e.g. '\"approve\"' or 'null').",
    )
    return parser.parse_args(argv)


def _load_answer(args: argparse.Namespace) -> Any:
    raw = sys.stdin.read() if args.answer_stdin else args.answer_json
    if raw is None or raw == "":
        raise ValueError("answer payload is empty; pass JSON via stdin or --answer-json")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"answer payload is not valid JSON: {exc}. The skill body must "
            "JSON-encode the operator's selection before invoking this "
            "helper (e.g. via `python3 -c 'import json,sys; "
            "sys.stdout.write(json.dumps(sys.stdin.read()))'`)."
        ) from exc


def _contract_path(state_root: Path, pipeline_id: str) -> Path:
    return state_root / "contracts" / f"{pipeline_id}.json"


def _read_contract(contract_path: Path) -> dict[str, Any]:
    if not contract_path.exists():
        raise FileNotFoundError(
            f"contract file does not exist at {contract_path}; run "
            "run_pipeline.py first to materialise the pending_hitl envelope."
        )
    try:
        data = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # fmt: skip
        raise RuntimeError(
            f"contract file at {contract_path} is unparseable: {exc}. Refusing "
            "to overwrite — the operator's accumulated answer_log would be "
            "silently dropped. Inspect / repair the contract file by hand."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"contract file at {contract_path} is not a JSON object "
            f"(got {type(data).__name__}); refusing to overwrite."
        )
    return data


def _write_contract_atomically(contract_path: Path, contract: dict[str, Any]) -> None:
    """Match run_pipeline.py's _write_contract: tmp + os.replace."""
    tmp = contract_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    os.replace(tmp, contract_path)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])

    try:
        answer = _load_answer(args)
    except ValueError as exc:
        print(f"write_answer.py: {exc}", file=sys.stderr)
        return 1

    state_root = Path(args.state_root) if args.state_root else Path.cwd() / ".egg-state"
    contract_path = _contract_path(state_root, args.pipeline_id)

    try:
        contract = _read_contract(contract_path)
    except (FileNotFoundError, RuntimeError) as exc:  # fmt: skip
        print(f"write_answer.py: {exc}", file=sys.stderr)
        return 1

    envelope = contract.get("pending_hitl")
    if not isinstance(envelope, dict):
        print(
            f"write_answer.py: contract at {contract_path} has no pending_hitl "
            "envelope to write into. Run run_pipeline.py first.",
            file=sys.stderr,
        )
        return 1

    envelope["answer"] = answer
    envelope["status"] = "answered"
    envelope["timestamp"] = _now_iso()
    contract["pending_hitl"] = envelope

    try:
        _write_contract_atomically(contract_path, contract)
    except OSError as exc:
        print(
            f"write_answer.py: failed to write contract at {contract_path}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"write_answer.py: status=answered pipeline_id={args.pipeline_id} "
        f"answer_type={type(answer).__name__}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
