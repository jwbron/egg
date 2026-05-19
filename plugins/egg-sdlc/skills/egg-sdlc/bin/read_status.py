#!/usr/bin/env python3
"""Read a single field from ``pending_hitl`` for the skill loop.

Companion to ``run_pipeline.py`` and ``write_answer.py``. The skill body
needs to branch on ``pending_hitl.status`` (and occasionally read
``pending_hitl.result`` / ``pending_hitl.error``) between driver
invocations. Earlier slices used an inline ``python3 -c "..."`` snippet
to do this, but that left the skill's ``allowed-tools`` having to
accept arbitrary ``python3 -c`` invocations — a prompt-injection
surface a malicious issue body could potentially coerce. This helper
exists so the skill loop can fence ``Bash(python3 …)`` to
``plugins/egg-sdlc/skills/egg-sdlc/bin/*`` and stay consistent with
the documented loop body.

Usage::

    python3 plugins/egg-sdlc/skills/egg-sdlc/bin/read_status.py \\
        --pipeline-id issue-1234 \\
        --field status                          # → "pending"

    python3 plugins/egg-sdlc/skills/egg-sdlc/bin/read_status.py \\
        --pipeline-id issue-1234 \\
        --field result                          # → ".egg-state/drafts/..."

Fields accepted: ``status``, ``result``, ``error``. The helper prints
the field's value to stdout (without quoting, so the skill body can
capture it with ``STATUS=$(python3 … --field status)`` and use it in a
shell ``case`` statement). A missing field prints an empty string and
exits ``0``. The skill body's ``case`` has no ``*)`` default arm by
design — an empty ``${STATUS}`` falls through cleanly and the skill's
outer iteration re-invokes ``run_pipeline.py``, which is the recover
path that re-materialises the ``pending_hitl`` envelope. (Don't change
the empty-status return to a non-zero exit; the fall-through is the
contract.) A missing or unparseable contract file exits ``1`` with a
diagnostic on stderr — same convention as ``write_answer.py``.

Exit codes:

* ``0`` — field read successfully (printed to stdout).
* ``1`` — argument error, missing contract, or unparseable contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ALLOWED_FIELDS = frozenset({"status", "result", "error"})


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="read_status.py",
        description=(
            "Print a single field from the contract's pending_hitl envelope "
            "(status / result / error). Companion to run_pipeline.py / "
            "write_answer.py."
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
    parser.add_argument(
        "--field",
        required=True,
        choices=sorted(_ALLOWED_FIELDS),
        help="Which pending_hitl field to print.",
    )
    return parser.parse_args(argv)


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
            f"contract file at {contract_path} is unparseable: {exc}. "
            "Inspect / repair the contract file by hand."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"contract file at {contract_path} is not a JSON object (got {type(data).__name__})."
        )
    return data


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])

    state_root = Path(args.state_root) if args.state_root else Path.cwd() / ".egg-state"
    contract_path = _contract_path(state_root, args.pipeline_id)

    try:
        contract = _read_contract(contract_path)
    except (FileNotFoundError, RuntimeError) as exc:  # fmt: skip
        print(f"read_status.py: {exc}", file=sys.stderr)
        return 1

    envelope = contract.get("pending_hitl")
    if not isinstance(envelope, dict):
        # No envelope yet → print empty and exit 0; the skill's case
        # statement treats this as "no decision pending" and re-invokes
        # the driver.
        print("")
        return 0

    value = envelope.get(args.field)
    if value is None:
        print("")
    else:
        print(str(value))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
