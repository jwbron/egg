"""Tests for the ``egg-orch overseer consult-advisor`` CLI verb (issue #1962).

The verb runs ``egg_overseer.advisor.consult_advisor`` sandbox-side so the
``run_agent_async`` Opus call lives on the LLM-execution side of the EGG200
boundary. These tests mock ``consult_advisor`` to avoid network / SDK use.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from egg_lib.orch_cli import cmd_overseer_consult_advisor, create_parser
from egg_overseer.advisor import AdvisorParseError, AdvisorVerdict

# ---------------------------------------------------------------------------
# argparse coverage
# ---------------------------------------------------------------------------


class TestOverseerConsultAdvisorParser:
    def test_parser_accepts_minimum_args(self) -> None:
        parser = create_parser()
        ns = parser.parse_args(
            [
                "overseer",
                "consult-advisor",
                "--inputs-file",
                "/tmp/inputs.json",
            ]
        )
        assert ns.command == "overseer"
        assert ns.overseer_command == "consult-advisor"
        assert ns.inputs_file == "/tmp/inputs.json"
        assert ns.output_file is None

    def test_parser_accepts_output_file(self) -> None:
        parser = create_parser()
        ns = parser.parse_args(
            [
                "overseer",
                "consult-advisor",
                "--inputs-file",
                "/tmp/inputs.json",
                "--output-file",
                "/tmp/verdict.json",
            ]
        )
        assert ns.output_file == "/tmp/verdict.json"

    def test_inputs_file_required(self) -> None:
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["overseer", "consult-advisor"])


# ---------------------------------------------------------------------------
# cmd_overseer_consult_advisor behaviour
# ---------------------------------------------------------------------------


def _make_args(
    *,
    inputs_file: str | Path,
    output_file: str | Path | None = None,
    json_flag: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        inputs_file=str(inputs_file),
        output_file=str(output_file) if output_file else None,
        json=json_flag,
    )


def _write_inputs(path: Path, *, data: object | None = None) -> None:
    if data is None:
        data = {
            "classification": {
                "type": "agent-loop",
                "confidence": 0.9,
                "reasoning": "thrashing on the same file",
            },
            "health_alerts": [{"type": "loop-detected", "detail": "5 identical commits"}],
            "progress_events": [{"step": "edit", "state": "working"}],
            "recent_log_lines": ["line 1", "line 2"],
        }
    path.write_text(json.dumps(data), encoding="utf-8")


class TestOverseerConsultAdvisorCommand:
    def test_missing_inputs_file_returns_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cmd_overseer_consult_advisor(_make_args(inputs_file="/nonexistent/inputs.json"))
        assert rc == 2
        assert "cannot read --inputs-file" in capsys.readouterr().err

    def test_invalid_json_inputs_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = tmp_path / "inputs.json"
        inputs.write_text("not json", encoding="utf-8")
        rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs))
        assert rc == 2
        assert "not valid JSON" in capsys.readouterr().err

    def test_inputs_must_be_object_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = tmp_path / "inputs.json"
        inputs.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs))
        assert rc == 2
        assert "must be a JSON object" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("key", "bad_value", "fragment"),
        [
            ("classification", [1, 2], "classification must be an object"),
            ("health_alerts", {"x": 1}, "health_alerts must be an array"),
            ("progress_events", "not a list", "progress_events must be an array"),
            ("recent_log_lines", 42, "recent_log_lines must be an array"),
        ],
    )
    def test_invalid_input_shape_returns_2(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        key: str,
        bad_value: object,
        fragment: str,
    ) -> None:
        data = {
            "classification": {},
            "health_alerts": [],
            "progress_events": [],
            "recent_log_lines": [],
            key: bad_value,
        }
        inputs = tmp_path / "inputs.json"
        _write_inputs(inputs, data=data)
        rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs))
        assert rc == 2
        assert fragment in capsys.readouterr().err

    def test_happy_path_writes_verdict_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = tmp_path / "inputs.json"
        _write_inputs(inputs)
        verdict = AdvisorVerdict(
            decision="alert",
            priority="p1",
            alert_summary="thrashing detected",
            alert_detail="5 identical commits in 60s",
            reasoning="haiku flagged loop, tier-1 alert active",
        )

        captured: dict[str, object] = {}

        async def _fake_consult_advisor(**kwargs: object) -> AdvisorVerdict:
            captured.update(kwargs)
            return verdict

        with patch(
            "egg_overseer.advisor.consult_advisor",
            side_effect=_fake_consult_advisor,
        ):
            rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs))
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["decision"] == "alert"
        assert payload["priority"] == "p1"
        assert payload["alert_summary"] == "thrashing detected"
        # Inputs are forwarded verbatim.
        assert captured["classification"] == {
            "type": "agent-loop",
            "confidence": 0.9,
            "reasoning": "thrashing on the same file",
        }
        assert captured["health_alerts"] == [
            {"type": "loop-detected", "detail": "5 identical commits"}
        ]
        assert captured["progress_events"] == [{"step": "edit", "state": "working"}]
        assert captured["recent_log_lines"] == ["line 1", "line 2"]
        assert captured["config"] is None

    def test_output_file_receives_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = tmp_path / "inputs.json"
        _write_inputs(inputs)
        out_path = tmp_path / "verdict.json"
        verdict = AdvisorVerdict(
            decision="watch",
            reasoning="no escalation needed",
        )

        async def _fake_consult_advisor(**_kwargs: object) -> AdvisorVerdict:
            return verdict

        with patch(
            "egg_overseer.advisor.consult_advisor",
            side_effect=_fake_consult_advisor,
        ):
            rc = cmd_overseer_consult_advisor(
                _make_args(inputs_file=inputs, output_file=out_path, json_flag=False)
            )
        assert rc == 0
        # Verdict landed in the file.
        written = json.loads(out_path.read_text(encoding="utf-8"))
        assert written["decision"] == "watch"
        assert written["reasoning"] == "no escalation needed"
        # Without --json, stdout reports the destination path.
        assert f"Wrote AdvisorVerdict to {out_path}" in capsys.readouterr().out

    def test_output_file_with_json_flag_also_prints_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = tmp_path / "inputs.json"
        _write_inputs(inputs)
        out_path = tmp_path / "verdict.json"
        verdict = AdvisorVerdict(
            decision="watch",
            reasoning="ok",
        )

        async def _fake_consult_advisor(**_kwargs: object) -> AdvisorVerdict:
            return verdict

        with patch(
            "egg_overseer.advisor.consult_advisor",
            side_effect=_fake_consult_advisor,
        ):
            rc = cmd_overseer_consult_advisor(
                _make_args(inputs_file=inputs, output_file=out_path, json_flag=True)
            )
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["decision"] == "watch"
        assert json.loads(out_path.read_text(encoding="utf-8"))["decision"] == "watch"

    def test_advisor_parse_error_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = tmp_path / "inputs.json"
        _write_inputs(inputs)

        async def _raise(**_kwargs: object) -> AdvisorVerdict:
            raise AdvisorParseError("bad payload from sdk")

        with patch(
            "egg_overseer.advisor.consult_advisor",
            side_effect=_raise,
        ):
            rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs))
        assert rc == 1
        assert "advisor parse failure" in capsys.readouterr().err

    def test_advisor_runtime_error_returns_3(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # SDK / network / auth / rate-limit failures get a distinct exit
        # code so the caller can distinguish them from a parse drift on
        # AdvisorVerdict (exit 1) and decide retry vs. classify-drift.
        inputs = tmp_path / "inputs.json"
        _write_inputs(inputs)

        async def _raise(**_kwargs: object) -> AdvisorVerdict:
            raise RuntimeError("connection reset by peer")

        with patch(
            "egg_overseer.advisor.consult_advisor",
            side_effect=_raise,
        ):
            rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs))
        assert rc == 3
        err = capsys.readouterr().err
        assert "advisor runtime failure" in err
        assert "RuntimeError" in err
        assert "connection reset by peer" in err

    def test_unwritable_output_file_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        inputs = tmp_path / "inputs.json"
        _write_inputs(inputs)
        # Path inside a non-existent directory triggers OSError on open().
        out_path = tmp_path / "nope" / "verdict.json"
        verdict = AdvisorVerdict(decision="watch", reasoning="ok")

        async def _fake(**_kwargs: object) -> AdvisorVerdict:
            return verdict

        with patch(
            "egg_overseer.advisor.consult_advisor",
            side_effect=_fake,
        ):
            rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs, output_file=out_path))
        assert rc == 2
        assert "cannot write --output-file" in capsys.readouterr().err

    def test_missing_optional_keys_default_to_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Only classification + health_alerts present; the verb should
        # default progress_events / recent_log_lines to [].
        inputs = tmp_path / "inputs.json"
        _write_inputs(
            inputs,
            data={
                "classification": {"type": "x", "confidence": 0.9, "reasoning": "y"},
                "health_alerts": [],
            },
        )
        captured: dict[str, object] = {}

        async def _fake(**kwargs: object) -> AdvisorVerdict:
            captured.update(kwargs)
            return AdvisorVerdict(decision="watch", reasoning="ok")

        with patch(
            "egg_overseer.advisor.consult_advisor",
            side_effect=_fake,
        ):
            rc = cmd_overseer_consult_advisor(_make_args(inputs_file=inputs))
        assert rc == 0
        assert captured["progress_events"] == []
        assert captured["recent_log_lines"] == []
