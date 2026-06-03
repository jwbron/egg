"""#2741 regression-guard tests for the prose-arg plumbing added in
slice-5 of issue #2908 (TASK-5-1).

The orchestrator's event-pump wrapper composes ``egg-orch consensus
{propose,ack,nack,withdraw}`` invocations from bash. Before slice-5 the
prose-bearing args (``--summary``, ``--reason``) only existed as argv
strings; when the wrapper substituted a multi-line summary or a NACK
reason containing shell metacharacters (``$VAR``, backticks, ``$()``,
``;``, ``&&``, embedded newlines), ``bash -c`` corrupted the value
before argv parsing — the failure mode #2741 mitigated for one verb at
a time.

Slice-5 generalises the fix: every prose-bearing arg now offers a
paired ``--FOO-file PATH`` flag and accepts ``-`` as the argv sentinel
for stdin. Argv prose still works for humans and during transition,
but emits ``DeprecationWarning`` so a regression to argv-only inside
the wrapper bash surfaces immediately.

These tests round-trip representative #2741 prose payloads through
each delivery channel and assert byte-equality between the on-disk /
stdin input and the request body received by the orchestrator
fake. The argv path tests also assert the deprecation warning fires.
"""

from __future__ import annotations

import argparse
import io
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_lib import orch_cli  # noqa: E402

# #2741 representative prose payloads — every one of these has been
# observed to break a naive ``bash -c "... --reason '$RAW' ..."``
# composition. We round-trip the literal value through each channel
# (file, stdin, argv) and assert byte-equality on the orchestrator
# fake's recorded request body.
PROSE_PAYLOADS = [
    pytest.param(
        "needs $VAR substitution but should NOT be expanded",
        id="dollar-var",
    ),
    pytest.param(
        "spans `multiple lines`\nand uses backticks",
        id="newline-and-backticks",
    ),
    pytest.param(
        "command-sub $(rm -rf /) must not run",
        id="command-sub",
    ),
    pytest.param("a; b && c || d", id="shell-control-ops"),
    pytest.param(
        "embedded newline\nthen tab\tthen end",
        id="newline-and-tab",
    ),
    pytest.param(
        "UTF-8 ✓ and emoji 🐣 should round-trip",
        id="utf8-non-ascii",
    ),
    pytest.param(
        # Single, double, and escape characters together.
        "she said \"don't 'do' this\" and \\then\\ left",
        id="quotes-and-escapes",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def brc_env(monkeypatch):
    """Standard agent-pod env."""
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-2908-impl2")
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator.test:9849")
    monkeypatch.delenv("EGG_LIFECYCLE_SECRET", raising=False)


def _ack_ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "role": None,
        "producer_role": "coder",
        "reason": None,
        "reason_file": None,
        "files_reviewed": ["a.py"],
        "files_reviewed_file": None,
        "pre_merge_condition": "",
        "pre_merge_condition_file": None,
        "pre_merge_condition_resolved_in_diff": "",
        "ack_version": 1,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _nack_ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "role": None,
        "producer_role": "coder",
        "reason": None,
        "reason_file": None,
        "files_reviewed": ["a.py"],
        "files_reviewed_file": None,
        "nack_version": 1,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _propose_ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "role": None,
        "file": None,
        "summary": None,
        "summary_file": None,
        "artifacts": [],
        "risk": None,
        "risk_file": None,
        "commit_sha": None,
        "changed_artifacts": None,
        "files_changed": [],
        "tests_run": [],
        "tasks": [],
        "push": False,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _withdraw_ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "role": None,
        "reason": None,
        "reason_file": None,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# task-5-1: --reason / --reason-file / stdin sentinel on consensus ack
# ---------------------------------------------------------------------------


class TestConsensusAckProseChannels:
    """``consensus ack --reason VALUE`` / ``--reason -`` / ``--reason-file PATH``.

    The ``--reason`` value is the prose payload most often corrupted by
    ``bash -c`` interpolation (#2741); ``--files-reviewed-file PATH``
    likewise carries an array of paths on disk so the wrapper can
    skip argv entirely.
    """

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_reason_file_round_trips_byte_equal(self, brc_env, payload, tmp_path):
        """``--reason-file PATH`` delivers the file contents byte-equal
        to the orchestrator. Covers the wrapper's preferred path."""
        reason_path = tmp_path / "reason.txt"
        reason_path.write_text(payload, encoding="utf-8")
        captured = {}

        def fake_brc_ack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake_brc_ack,
        ):
            rc = orch_cli.cmd_consensus_ack(
                _ack_ns(reason_file=str(reason_path)),
            )
        assert rc == 0
        assert captured["reason"] == payload, (
            f"--reason-file payload mismatch: got {captured['reason']!r} expected {payload!r}"
        )

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_reason_stdin_sentinel_round_trips_byte_equal(self, brc_env, payload, monkeypatch):
        """``--reason -`` reads from stdin verbatim."""
        captured = {}

        def fake_brc_ack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake_brc_ack,
        ):
            rc = orch_cli.cmd_consensus_ack(_ack_ns(reason="-"))
        assert rc == 0
        assert captured["reason"] == payload

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_reason_argv_emits_deprecation_warning(self, brc_env, payload):
        """argv ``--reason "STRING"`` still works but warns. The warning
        is what makes a regression to argv-only inside the wrapper bash
        surface — test rigs can flip ``-W error::DeprecationWarning`` to
        promote it to a hard failure."""
        captured = {}

        def fake_brc_ack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_ack",
                side_effect=fake_brc_ack,
            ):
                rc = orch_cli.cmd_consensus_ack(_ack_ns(reason=payload))
        assert rc == 0
        assert captured["reason"] == payload
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert depr, (
            "argv --reason must emit DeprecationWarning so a regression "
            "to argv-only inside the wrapper bash surfaces immediately"
        )
        assert "--reason" in str(depr[0].message)

    def test_reason_and_reason_file_mutually_exclusive(self, brc_env, tmp_path, capsys):
        """Passing both ``--reason`` and ``--reason-file`` is a hard
        error — silent drop of one channel would invite composition-site
        bugs. cmd_* returns rc=2 (the established orch_cli pattern for
        argument-validation failures — see `cmd_consensus_ack`'s
        `--pre-merge-condition-resolved-in-diff` guard)."""
        reason_path = tmp_path / "reason.txt"
        reason_path.write_text("from-file", encoding="utf-8")
        rc = orch_cli.cmd_consensus_ack(
            _ack_ns(reason="from-argv", reason_file=str(reason_path)),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "mutually exclusive" in err

    def test_missing_reason_fails_cleanly(self, brc_env, capsys):
        """No reason channel set → cmd_* returns rc=2 with a helpful
        error message on stderr (no SystemExit, no traceback)."""
        rc = orch_cli.cmd_consensus_ack(_ack_ns(reason=None, reason_file=None))
        assert rc == 2
        err = capsys.readouterr().err
        assert "--reason" in err


class TestConsensusAckFilesReviewedFile:
    """``consensus ack --files-reviewed-file PATH`` — one path per line.

    Per architect v2 §verification_strategy.slice_5: one path per line,
    blank lines and lines starting with ``#`` are stripped so a wrapper
    can generate a manifest with comments.
    """

    def test_files_reviewed_file_one_path_per_line(self, brc_env, tmp_path):
        """Each non-blank, non-comment line becomes an entry."""
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(
            "# preamble — comment line\n"
            "sandbox/egg_lib/orch_cli.py\n"
            "\n"  # blank
            "tests/sandbox/egg_lib/test_orch_cli_prose_args.py\n"
            "# trailing comment\n"
            "shared/egg_agent/client.py\n",
            encoding="utf-8",
        )
        captured = {}

        def fake_brc_ack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake_brc_ack,
        ):
            rc = orch_cli.cmd_consensus_ack(
                _ack_ns(
                    reason="manifest-test-reason",
                    files_reviewed=None,
                    files_reviewed_file=str(manifest),
                ),
            )
        assert rc == 0
        assert captured["files_reviewed"] == [
            "sandbox/egg_lib/orch_cli.py",
            "tests/sandbox/egg_lib/test_orch_cli_prose_args.py",
            "shared/egg_agent/client.py",
        ]

    def test_files_reviewed_and_file_mutually_exclusive(self, brc_env, tmp_path, capsys):
        manifest = tmp_path / "m.txt"
        manifest.write_text("a.py\n", encoding="utf-8")
        rc = orch_cli.cmd_consensus_ack(
            _ack_ns(
                reason="r",
                files_reviewed=["b.py"],
                files_reviewed_file=str(manifest),
            ),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "mutually exclusive" in err

    def test_missing_files_reviewed_fails_cleanly(self, brc_env, capsys):
        """No files-reviewed channel set → return 2 with a helpful error
        message on stderr. (Returned, not raised — the cmd_* functions
        use rc=2 for argument-validation failures so callers can choose
        their own exit semantics.)"""
        rc = orch_cli.cmd_consensus_ack(
            _ack_ns(reason="r", files_reviewed=None, files_reviewed_file=None),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "--files-reviewed" in err


# ---------------------------------------------------------------------------
# task-5-1: --reason / --reason-file / stdin sentinel on consensus nack
# ---------------------------------------------------------------------------


class TestConsensusNackProseChannels:
    """NACK mirrors ACK's prose-arg plumbing (#2741 affects both verbs
    equally — adversarial review prose is even more likely to contain
    shell metacharacters)."""

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_reason_file_round_trips_byte_equal(self, brc_env, payload, tmp_path):
        reason_path = tmp_path / "r.txt"
        reason_path.write_text(payload, encoding="utf-8")
        captured = {}

        def fake_brc_nack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with patch(
            "egg_agent_tools.handlers.brc.brc_nack",
            side_effect=fake_brc_nack,
        ):
            rc = orch_cli.cmd_consensus_nack(_nack_ns(reason_file=str(reason_path)))
        assert rc == 0
        assert captured["reason"] == payload

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_reason_stdin_sentinel_round_trips_byte_equal(self, brc_env, payload, monkeypatch):
        captured = {}

        def fake_brc_nack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with patch(
            "egg_agent_tools.handlers.brc.brc_nack",
            side_effect=fake_brc_nack,
        ):
            rc = orch_cli.cmd_consensus_nack(_nack_ns(reason="-"))
        assert rc == 0
        assert captured["reason"] == payload

    def test_reason_argv_emits_deprecation_warning(self, brc_env):
        captured = {}

        def fake_brc_nack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_nack",
                side_effect=fake_brc_nack,
            ):
                rc = orch_cli.cmd_consensus_nack(_nack_ns(reason="argv-prose"))
        assert rc == 0
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert depr and "--reason" in str(depr[0].message)


# ---------------------------------------------------------------------------
# task-5-1: --summary / --summary-file on consensus propose
# ---------------------------------------------------------------------------


class TestConsensusProposeSummaryChannels:
    """``consensus propose --summary`` / ``--summary -`` / ``--summary-file``."""

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_summary_file_round_trips_byte_equal(self, brc_env, payload, tmp_path):
        sf = tmp_path / "s.txt"
        sf.write_text(payload, encoding="utf-8")
        captured = {}

        def fake_brc_propose(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with patch(
            "egg_agent_tools.handlers.brc.brc_propose",
            side_effect=fake_brc_propose,
        ):
            rc = orch_cli.cmd_consensus_propose(_propose_ns(summary_file=str(sf)))
        assert rc == 0
        assert captured["summary"] == payload

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_summary_stdin_sentinel_round_trips_byte_equal(self, brc_env, payload, monkeypatch):
        captured = {}

        def fake_brc_propose(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with patch(
            "egg_agent_tools.handlers.brc.brc_propose",
            side_effect=fake_brc_propose,
        ):
            rc = orch_cli.cmd_consensus_propose(_propose_ns(summary="-"))
        assert rc == 0
        assert captured["summary"] == payload

    def test_summary_argv_emits_deprecation_warning(self, brc_env):
        captured = {}

        def fake_brc_propose(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_propose",
                side_effect=fake_brc_propose,
            ):
                rc = orch_cli.cmd_consensus_propose(_propose_ns(summary="argv-summary"))
        assert rc == 0
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert depr and "--summary" in str(depr[0].message)

    def test_file_payload_path_unchanged_no_warning(self, brc_env, tmp_path):
        """``consensus propose --file`` (the existing JSON-payload path
        from issue #1738) must continue to work and must NOT emit a
        deprecation warning — only the per-arg argv channels are
        deprecated, not the structured payload-file path."""
        import json

        payload_path = tmp_path / "payload.json"
        payload_path.write_text(
            json.dumps({"summary": "from json file"}),
            encoding="utf-8",
        )
        captured = {}

        def fake_brc_propose(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_propose",
                side_effect=fake_brc_propose,
            ):
                rc = orch_cli.cmd_consensus_propose(_propose_ns(file=str(payload_path)))
        assert rc == 0
        # The handler is invoked with ``raw_payload`` containing the
        # JSON dict — confirm the unchanged shape.
        assert captured.get("raw_payload", {}).get("summary") == "from json file"
        # No deprecation warning on the file-payload path.
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert not depr, (
            "consensus propose --file (JSON payload) must NOT emit a "
            "deprecation warning — only per-arg argv channels are deprecated"
        )


class TestConsensusProposeRiskChannels:
    """``consensus propose --risk`` / ``--risk -`` / ``--risk-file``.

    Risk prose is exactly the kind of content where a reviewer NACK
    might quote shell-hazard descriptions (e.g. backticked
    ``git reset --hard`` or ``; rm -rf /``) — the same #2741
    shell-metachar plumbing applies.
    """

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_risk_file_round_trips_byte_equal(self, brc_env, payload, tmp_path):
        rf = tmp_path / "risk.txt"
        rf.write_text(payload, encoding="utf-8")
        captured = {}

        def fake_brc_propose(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with patch(
            "egg_agent_tools.handlers.brc.brc_propose",
            side_effect=fake_brc_propose,
        ):
            rc = orch_cli.cmd_consensus_propose(_propose_ns(risk_file=str(rf)))
        assert rc == 0
        assert captured["risk_considered"] == payload

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_risk_stdin_sentinel_round_trips_byte_equal(self, brc_env, payload, monkeypatch):
        captured = {}

        def fake_brc_propose(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with patch(
            "egg_agent_tools.handlers.brc.brc_propose",
            side_effect=fake_brc_propose,
        ):
            rc = orch_cli.cmd_consensus_propose(_propose_ns(risk="-"))
        assert rc == 0
        assert captured["risk_considered"] == payload

    def test_risk_argv_emits_deprecation_warning(self, brc_env):
        captured = {}

        def fake_brc_propose(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_propose",
                side_effect=fake_brc_propose,
            ):
                rc = orch_cli.cmd_consensus_propose(_propose_ns(risk="argv-risk"))
        assert rc == 0
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert depr and any("--risk" in str(w.message) for w in depr)


class TestConsensusAckPreMergeConditionChannels:
    """``consensus ack --pre-merge-condition`` / ``-`` / ``--pre-merge-condition-file``.

    The obligation prose frequently quotes shell commands the wrapper
    bash would otherwise corrupt (e.g. ``git mv legacy/auth.py
    src/auth.py``); same plumbing as ``--reason``.
    """

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_pre_merge_condition_file_round_trips_byte_equal(self, brc_env, payload, tmp_path):
        pmc_path = tmp_path / "pmc.txt"
        pmc_path.write_text(payload, encoding="utf-8")
        reason_path = tmp_path / "reason.txt"
        reason_path.write_text("approved with obligation", encoding="utf-8")
        captured = {}

        def fake_brc_ack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake_brc_ack,
        ):
            rc = orch_cli.cmd_consensus_ack(
                _ack_ns(
                    reason_file=str(reason_path),
                    pre_merge_condition_file=str(pmc_path),
                ),
            )
        assert rc == 0
        assert captured["pre_merge_condition"] == payload

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_pre_merge_condition_stdin_sentinel_round_trips_byte_equal(
        self, brc_env, payload, monkeypatch, tmp_path
    ):
        reason_path = tmp_path / "reason.txt"
        reason_path.write_text("approved with obligation", encoding="utf-8")
        captured = {}

        def fake_brc_ack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake_brc_ack,
        ):
            rc = orch_cli.cmd_consensus_ack(
                _ack_ns(
                    reason_file=str(reason_path),
                    pre_merge_condition="-",
                ),
            )
        assert rc == 0
        assert captured["pre_merge_condition"] == payload

    def test_pre_merge_condition_argv_emits_deprecation_warning(self, brc_env, tmp_path):
        reason_path = tmp_path / "reason.txt"
        reason_path.write_text("approved with obligation", encoding="utf-8")
        captured = {}

        def fake_brc_ack(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_ack",
                side_effect=fake_brc_ack,
            ):
                rc = orch_cli.cmd_consensus_ack(
                    _ack_ns(
                        reason_file=str(reason_path),
                        pre_merge_condition="argv-pmc",
                    ),
                )
        assert rc == 0
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert depr and any("--pre-merge-condition" in str(w.message) for w in depr)

    def test_default_empty_pre_merge_condition_emits_no_warning(self, brc_env, tmp_path):
        """The argparse default ``pre_merge_condition=""`` (no flag
        passed) must not emit a spurious deprecation warning — only an
        explicitly supplied argv value should warn."""
        reason_path = tmp_path / "reason.txt"
        reason_path.write_text("unconditional approval", encoding="utf-8")

        def fake_brc_ack(req):
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_ack",
                side_effect=fake_brc_ack,
            ):
                rc = orch_cli.cmd_consensus_ack(
                    _ack_ns(reason_file=str(reason_path)),
                )
        assert rc == 0
        pmc_depr = [
            w
            for w in warnlog
            if issubclass(w.category, DeprecationWarning)
            and "--pre-merge-condition" in str(w.message)
        ]
        assert not pmc_depr, "default empty --pre-merge-condition must not emit deprecation"


# ---------------------------------------------------------------------------
# task-5-1: --reason / --reason-file on consensus withdraw
# ---------------------------------------------------------------------------


class TestConsensusWithdrawReasonChannels:
    """Withdraw uses the same prose-arg plumbing as ack/nack but hits
    a different code path (the legacy ``/api/v1/pipelines/{pid}/signal``
    endpoint instead of the handler layer)."""

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_reason_file_round_trips_byte_equal(self, brc_env, payload, tmp_path):
        reason_path = tmp_path / "r.txt"
        reason_path.write_text(payload, encoding="utf-8")
        captured = {}

        def fake_orch_request(endpoint, *, method="GET", data=None):
            captured["data"] = data
            return {"success": True}

        with patch(
            "egg_lib.orch_cli.orch_request",
            side_effect=fake_orch_request,
        ):
            rc = orch_cli.cmd_consensus_withdraw(
                _withdraw_ns(reason_file=str(reason_path)),
            )
        assert rc == 0
        assert captured["data"]["reason"] == payload

    @pytest.mark.parametrize("payload", PROSE_PAYLOADS)
    def test_reason_stdin_sentinel_round_trips_byte_equal(self, brc_env, payload, monkeypatch):
        captured = {}

        def fake_orch_request(endpoint, *, method="GET", data=None):
            captured["data"] = data
            return {"success": True}

        monkeypatch.setattr("sys.stdin", io.StringIO(payload))
        with patch(
            "egg_lib.orch_cli.orch_request",
            side_effect=fake_orch_request,
        ):
            rc = orch_cli.cmd_consensus_withdraw(_withdraw_ns(reason="-"))
        assert rc == 0
        assert captured["data"]["reason"] == payload

    def test_reason_argv_emits_deprecation_warning(self, brc_env):
        captured = {}

        def fake_orch_request(endpoint, *, method="GET", data=None):
            captured["data"] = data
            return {"success": True}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_lib.orch_cli.orch_request",
                side_effect=fake_orch_request,
            ):
                rc = orch_cli.cmd_consensus_withdraw(
                    _withdraw_ns(reason="argv-withdraw"),
                )
        assert rc == 0
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert depr and "--reason" in str(depr[0].message)


# ---------------------------------------------------------------------------
# task-5-1 parser help-text smoke (advertises new flags)
# ---------------------------------------------------------------------------


class TestParserHelpAdvertisesNewFlags:
    """``--help`` for each verb advertises the new file/stdin channels
    so wrapper authors can discover them without grepping source."""

    @pytest.mark.parametrize(
        ("argv", "must_contain"),
        [
            (
                ["consensus", "ack", "--help"],
                [
                    "--reason-file",
                    "--files-reviewed-file",
                    "--pre-merge-condition-file",
                ],
            ),
            (["consensus", "nack", "--help"], ["--reason-file", "--files-reviewed-file"]),
            (["consensus", "withdraw", "--help"], ["--reason-file"]),
            (
                ["consensus", "propose", "--help"],
                ["--summary-file", "--risk-file"],
            ),
        ],
    )
    def test_help_lists_new_flags(self, brc_env, capsys, argv, must_contain):
        parser = orch_cli.create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
        out = capsys.readouterr().out
        for needle in must_contain:
            assert needle in out, (
                f"argv {argv!r}: --help output missing {needle!r}; "
                "wrapper authors will not discover the new channel"
            )
