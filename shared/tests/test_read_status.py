"""Unit tests for ``plugins/egg-sdlc/skills/egg-sdlc/bin/read_status.py``.

The helper is part of the skill loop's documented surface (#2717
slice-1): the skill body reads ``pending_hitl.status`` between driver
invocations to decide whether to render via ``AskUserQuestion``, wait,
or exit. Earlier slices used an inline ``python3 -c "..."`` snippet for
this read, but that left the skill's ``allowed-tools`` having to accept
arbitrary ``python3 -c`` invocations (prompt-injection surface). The
helper exists so each subcommand in the loop body is a single
``python3 plugins/.../bin/<helper>.py`` invocation that matches the
``allowed-tools`` pattern independently per Claude Code's compound-
command rules.

Tests cover:

* Reading ``status`` from a valid envelope prints the value to stdout.
* Reading ``result`` / ``error`` works identically.
* A missing envelope prints an empty string and exits 0 (the skill's
  ``case`` statement falls through cleanly).
* A missing contract file exits 1 with a diagnostic on stderr.
* An unparseable contract file exits 1 — does NOT silently print the
  default skeleton's value.
"""

from __future__ import annotations

import importlib.util
import io
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
    / "read_status.py"
)


def _load_helper_module() -> Any:
    spec = importlib.util.spec_from_file_location("egg_sdlc_read_status", _HELPER_PATH)
    assert spec is not None and spec.loader is not None, f"could not load {_HELPER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper() -> Any:
    return _load_helper_module()


def _seed_contract(
    tmp_path: Path,
    *,
    pipeline_id: str = "issue-test",
    status: str = "pending",
    result: str | None = None,
    error: str | None = None,
) -> Path:
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
                    "status": status,
                    "result": result,
                    "error": error,
                    "answer_log": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return contract_path


def test_reads_status_pending(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_contract(tmp_path, status="pending")
    monkeypatch.chdir(tmp_path)
    rc = helper.main(["--pipeline-id", "issue-test", "--field", "status"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "pending"


def test_reads_status_completed(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_contract(tmp_path, status="completed", result="/path/to/analysis.md")
    monkeypatch.chdir(tmp_path)
    rc = helper.main(["--pipeline-id", "issue-test", "--field", "result"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "/path/to/analysis.md"


def test_reads_error(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_contract(tmp_path, status="error", error="something went wrong")
    monkeypatch.chdir(tmp_path)
    rc = helper.main(["--pipeline-id", "issue-test", "--field", "error"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "something went wrong"


def test_missing_envelope_prints_empty(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A contract without a ``pending_hitl`` envelope is not an error.

    The skill's ``case`` statement must fall through cleanly when there's
    no decision pending — printing an empty string + exit 0 is the
    contract.
    """
    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    contract_path = contracts / "issue-test.json"
    contract_path.write_text(json.dumps({"pipeline_id": "issue-test"}))
    monkeypatch.chdir(tmp_path)

    rc = helper.main(["--pipeline-id", "issue-test", "--field", "status"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == ""


def test_missing_contract_exits_nonzero(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = helper.main(["--pipeline-id", "issue-test", "--field", "status"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "does not exist" in captured.err


def test_unparseable_contract_exits_nonzero(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unparseable contract → exit 1, do NOT silently emit a default value."""
    contracts = tmp_path / ".egg-state" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    contract_path = contracts / "issue-test.json"
    contract_path.write_text("{ not valid json")
    monkeypatch.chdir(tmp_path)

    rc = helper.main(["--pipeline-id", "issue-test", "--field", "status"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "unparseable" in captured.err


def test_rejects_disallowed_field(
    helper: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """argparse choices restrict ``--field`` to the known set."""
    _seed_contract(tmp_path)
    monkeypatch.chdir(tmp_path)
    # ``answer`` is intentionally NOT in the allowed set — reads of the
    # operator's answer field would expose a prompt-injection surface
    # the helper has no reason to support.
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # silence argparse stderr
    with pytest.raises(SystemExit) as excinfo:
        helper.main(["--pipeline-id", "issue-test", "--field", "answer"])
    assert excinfo.value.code != 0
