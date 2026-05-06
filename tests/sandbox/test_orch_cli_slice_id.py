"""Fail-fast EGG_SLICE_ID validation in egg_lib.orch_cli (#2473).

CLI commands that forward ``slice_id`` should reject a misconfigured
``EGG_SLICE_ID`` locally — surfacing a clear error to stderr — instead
of round-tripping a 400 through the orchestrator's
``slice_id_validation.extract_slice_id``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib import orch_cli


class TestResolveSliceId:
    def test_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        assert orch_cli.resolve_slice_id() is None

    def test_returns_none_when_empty(self, monkeypatch):
        # Empty string is treated as unset — mirrors get_slice_id_from_env.
        monkeypatch.setenv("EGG_SLICE_ID", "")
        assert orch_cli.resolve_slice_id() is None

    def test_returns_validated_value(self, monkeypatch):
        monkeypatch.setenv("EGG_SLICE_ID", "slice-7")
        assert orch_cli.resolve_slice_id() == "slice-7"

    @pytest.mark.parametrize(
        "bad",
        [
            "slice-2/../etc",
            "phase-2",
            "slice-",
            "slice-2a",
            "../slice-2",
        ],
    )
    def test_invalid_value_exits(self, monkeypatch, capsys, bad):
        monkeypatch.setenv("EGG_SLICE_ID", bad)
        with pytest.raises(SystemExit) as exc:
            orch_cli.resolve_slice_id()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "EGG_SLICE_ID" in err
        assert "slice-<N>" in err


class TestConsensusWithdrawForwardsSliceId:
    """``cmd_consensus_withdraw`` validates ``EGG_SLICE_ID`` before posting."""

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            pipeline_id="issue-2473",
            role="coder",
            reason="superseded by v2",
            json=False,
        )

    def test_attaches_slice_id_from_env(self, monkeypatch):
        monkeypatch.setenv("EGG_SLICE_ID", "slice-3")
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={"success": True},
        ) as req:
            rc = orch_cli.cmd_consensus_withdraw(self._args())
        assert rc == 0
        assert req.call_args.kwargs["data"]["slice_id"] == "slice-3"

    def test_omits_slice_id_when_unset(self, monkeypatch):
        monkeypatch.delenv("EGG_SLICE_ID", raising=False)
        with patch(
            "egg_lib.orch_cli.orch_request",
            return_value={"success": True},
        ) as req:
            rc = orch_cli.cmd_consensus_withdraw(self._args())
        assert rc == 0
        assert "slice_id" not in req.call_args.kwargs["data"]

    def test_invalid_slice_id_fails_fast(self, monkeypatch, capsys):
        # Misconfigured env var must exit before any orchestrator call.
        monkeypatch.setenv("EGG_SLICE_ID", "slice-2/../etc")
        with patch("egg_lib.orch_cli.orch_request") as req:
            with pytest.raises(SystemExit) as exc:
                orch_cli.cmd_consensus_withdraw(self._args())
        assert exc.value.code == 1
        assert not req.called, "fail-fast must short-circuit before the request"
        assert "EGG_SLICE_ID" in capsys.readouterr().err
