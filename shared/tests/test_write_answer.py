"""Unit tests for ``plugins/egg-sdlc/skills/egg-sdlc/bin/write_answer.py``.

The helper is load-bearing for the flattened-bridge skill loop (#2717
slice-1): it ferries the operator's answer from the
``AskUserQuestion``-rendered selection into ``pending_hitl.answer`` of
the contract file. Earlier slices documented this as an inline
``python3 -c "..."`` snippet that was broken in three independent ways
(shell-interpolated answer, deprecated ``datetime.datetime.utcnow``,
non-atomic write); the helper exists so the load-bearing piece is
read-reviewable next to ``run_pipeline.py`` and exercised by tests.

Tests cover:

* JSON-encoded answer on stdin survives shell quoting (the original
  blocker: ``approve`` resolved to a Python ``NameError`` when
  shell-interpolated).
* Timestamp uses ``datetime.now(UTC).isoformat()`` — matches
  ``run_pipeline.py``'s ``_now_iso`` so the driver and helper never
  drift on timestamp shape.
* Atomic write via tmp + ``os.replace`` (never observed half-written).
* Refuses to overwrite a corrupted contract file (would silently drop
  ``answer_log``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_HELPER_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "egg-sdlc"
    / "skills"
    / "egg-sdlc"
    / "bin"
    / "write_answer.py"
)


def _load_helper_module() -> Any:
    """Import ``write_answer.py`` as a module without altering sys.path.

    The helper lives outside the regular package tree (under
    ``plugins/egg-sdlc/skills/egg-sdlc/bin/``) so a normal ``import
    write_answer`` does not find it. ``importlib.util.spec_from_file_location``
    is the standard way to load a one-off script-as-module without
    polluting ``sys.path``.
    """
    spec = importlib.util.spec_from_file_location("egg_sdlc_write_answer", _HELPER_PATH)
    assert spec is not None and spec.loader is not None, f"could not load {_HELPER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper() -> Any:
    return _load_helper_module()


def _seed_contract(tmp_path: Path, pipeline_id: str = "issue-test") -> Path:
    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    contract_path = contracts / f"{pipeline_id}.json"
    contract_path.write_text(
        json.dumps(
            {
                "pipeline_id": pipeline_id,
                "pending_hitl": {
                    "version": 1,
                    "pipeline_id": pipeline_id,
                    "timestamp": "2025-01-01T00:00:00+00:00",
                    "decision": {"question": "?", "options": []},
                    "answer": None,
                    "status": "pending",
                    "result": None,
                    "error": None,
                    "answer_log": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return contract_path


def test_stdin_json_answer_round_trips(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper reads a JSON-encoded answer from stdin and writes it.

    Pins the fix for the original blocker: ``approve`` (a bare string
    that would shell-interpolate into Python source as a ``NameError``)
    is correctly JSON-decoded back to the literal string ``"approve"``
    before being assigned to ``pending_hitl.answer``.
    """
    contract_path = _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    # Simulate the skill body's ``printf '%s' "${ANSWER}" | python3 -c
    # 'json.dumps(stdin)'`` step by writing the JSON-encoded answer.
    monkeypatch.setattr(sys, "stdin", _StubStdin(json.dumps("approve")))

    rc = helper.main(["--pipeline-id", "issue-test", "--answer-stdin"])
    assert rc == 0
    data = json.loads(contract_path.read_text())
    assert data["pending_hitl"]["answer"] == "approve"
    assert data["pending_hitl"]["status"] == "answered"


def test_answer_string_flag_round_trips(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--answer-string`` passes the raw selection; helper JSON-encodes internally.

    Pins the loop-body contract: the skill body calls
    ``write_answer.py --answer-string "${ANSWER}"`` directly. The
    helper takes the raw selection (no separate ``json.dumps``
    subcommand needed) and writes the literal string into
    ``pending_hitl.answer``. This is the path that lets the skill's
    ``allowed-tools`` fence ``python3`` to ``bin/*`` without leaving a
    ``Bash(python3 -c …)`` hole for the json.dumps step.
    """
    contract_path = _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = helper.main(["--pipeline-id", "issue-test", "--answer-string", "approve"])
    assert rc == 0
    data = json.loads(contract_path.read_text())
    assert data["pending_hitl"]["answer"] == "approve"
    assert data["pending_hitl"]["status"] == "answered"


def test_answer_string_special_characters(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--answer-string`` survives shell-special characters in the answer.

    Pins the shell-quoting invariant: the helper does NOT re-parse the
    string as Python source, so ``"approve & continue"`` or
    ``'"abort"'`` lands as the literal string in ``pending_hitl.answer``.
    """
    contract_path = _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    tricky = 'approve & "continue" $now'
    rc = helper.main(["--pipeline-id", "issue-test", "--answer-string", tricky])
    assert rc == 0
    data = json.loads(contract_path.read_text())
    assert data["pending_hitl"]["answer"] == tricky


def test_answer_json_flag_round_trips(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--answer-json`` accepts the JSON literal directly (no stdin)."""
    contract_path = _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = helper.main(
        [
            "--pipeline-id",
            "issue-test",
            "--answer-json",
            json.dumps({"selected": "approve"}),
        ]
    )
    assert rc == 0
    data = json.loads(contract_path.read_text())
    assert data["pending_hitl"]["answer"] == {"selected": "approve"}
    assert data["pending_hitl"]["status"] == "answered"


def test_timestamp_format_matches_driver(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timestamp uses ``datetime.now(UTC).isoformat()`` — no trailing ``Z``.

    Pins the contract that helper and driver write the same timestamp
    shape. The driver's ``_now_iso`` (``run_pipeline.py:101-103``)
    produces ``...+00:00`` (no ``Z``); a regression that adds ``Z``
    here would produce a mis-formatted ISO-8601 string when both
    writers stamp the envelope in alternation.
    """
    contract_path = _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = helper.main(["--pipeline-id", "issue-test", "--answer-json", json.dumps("approve")])
    assert rc == 0
    data = json.loads(contract_path.read_text())
    timestamp = data["pending_hitl"]["timestamp"]
    assert timestamp.endswith("+00:00"), (
        f"timestamp must end with '+00:00' (matching driver's _now_iso); "
        f"got {timestamp!r}. A trailing 'Z' suffix would diverge from the "
        f"driver's writes and produce mis-formatted ISO-8601."
    )
    assert "Z" not in timestamp, f"timestamp must not contain 'Z'; got {timestamp!r}"


def test_refuses_to_overwrite_corrupted_contract(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present-but-unparseable contract is NOT silently overwritten.

    Pins the fix for the silent-fallback bug: an earlier ``_read_contract``
    swallowed ``JSONDecodeError`` and returned a fresh skeleton, dropping
    ``answer_log`` on the floor. The helper now refuses to write so the
    operator's accumulated answers are preserved for hand-repair.
    """
    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    contract_path = contracts / "issue-test.json"
    contract_path.write_text("{ this is not valid JSON")
    monkeypatch.chdir(tmp_path)

    rc = helper.main(["--pipeline-id", "issue-test", "--answer-json", json.dumps("approve")])
    assert rc == 1
    # The corrupted file is preserved verbatim — no silent overwrite.
    assert contract_path.read_text() == "{ this is not valid JSON"


def test_invalid_json_answer_exits_nonzero(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--answer-json`` payload must be valid JSON; otherwise exit 1.

    The shell-side ``printf '%s' "${ANSWER}" | json.dumps`` step is the
    skill body's responsibility. If a future regression in the skill
    loop drops the json.dumps wrapper, the helper exits nonzero rather
    than corrupting the envelope with a Python ``NameError``-equivalent
    payload.
    """
    _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = helper.main(["--pipeline-id", "issue-test", "--answer-json", "this is not json"])
    assert rc == 1


def test_atomic_write_uses_tmp_then_replace(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper writes via tmp + os.replace, not a truncating open.

    Pins the contract: a crash mid-write must NOT leave a half-written
    contract for the driver's ``_read_contract`` to choke on. We
    monkeypatch ``os.replace`` to confirm it's invoked with the tmp
    path that the helper itself constructed (``.json.tmp`` suffix —
    matching the driver's ``_write_contract`` shape).
    """
    contract_path = _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)

    captured: dict[str, Path] = {}

    real_replace = helper.os.replace

    def fake_replace(src: str, dst: str) -> None:
        captured["src"] = Path(src)
        captured["dst"] = Path(dst)
        real_replace(src, dst)

    monkeypatch.setattr(helper.os, "replace", fake_replace)

    rc = helper.main(["--pipeline-id", "issue-test", "--answer-json", json.dumps("approve")])
    assert rc == 0
    assert captured["src"].name.endswith(".json.tmp"), (
        f"helper must write to tmp + os.replace; got src={captured.get('src')!r}"
    )
    assert captured["dst"] == contract_path


class _StubStdin:
    """Minimal ``sys.stdin`` stand-in that returns a fixed string from ``.read()``."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self) -> str:
        return self._payload
