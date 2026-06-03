"""Adversarial probes for the prose-arg plumbing and the new
``brc resolve-obligation`` / ``brc read-peer-artifact`` CLI subcommands
added in #2908 slice-5 (TASK-5-1, TASK-5-2, TASK-5-3).

The coder-authored test suite covers the happy-path round-trips and
the documented mutual-exclusion guards. These probes target edges and
boundary conditions that suite does not exercise:

* file-read error paths (non-UTF-8 content, missing path) — the
  coder catches ``OSError`` but ``UnicodeDecodeError`` derives from
  ``ValueError`` and slips through unless explicitly caught
* one-path-per-line semantics edge cases (comments-only manifest,
  trailing-newline preservation, leading-whitespace stripping)
* empty-channel semantics (``--reason -`` with empty stdin,
  ``--reason ""`` empty argv)
* ``brc read-peer-artifact`` argument-validation gaps that get
  forwarded to the handler (``--limit 0`` / ``-1``, ``--message-type``
  repeated 3+ times)
* ``brc resolve-obligation`` semantics (stdout format on happy path,
  handler-side rejection of resolver==producer, ``--json`` output
  shape)

Each test that surfaces a real bug is marked in its docstring so the
coder's NACK rationale can name it; the rest pin behaviour against
silent regressions.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers.errors import HandlerError  # noqa: E402
from egg_lib import orch_cli  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def brc_env(monkeypatch):
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-2908-impl2")
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_ORCHESTRATOR_URL", "http://orchestrator.test:9849")
    monkeypatch.delenv("EGG_LIFECYCLE_SECRET", raising=False)
    monkeypatch.delenv("EGG_SLICE_ID", raising=False)


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


def _resolve_ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "role": None,
        "reviewer_role": "reviewer_contract",
        "producer_role": "coder",
        "commit_sha": None,
        "note": None,
        "note_file": None,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _read_ns(**overrides):
    defaults = {
        "pipeline_id": None,
        "phase": "implement",
        "peer_role": None,
        "message_type": None,
        "limit": None,
        "cursor": None,
        "include_unattributed": True,
        "json": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Adversarial probes: prose-arg file-read error handling
# ---------------------------------------------------------------------------


class TestProseFileReadErrors:
    """File-read paths that should surface a clean CLI error rather
    than a Python traceback.

    The coder's ``_resolve_prose_arg`` catches ``OSError`` (missing
    path, permission denied) but ``UnicodeDecodeError`` derives from
    ``ValueError`` — a binary file slips through and produces a raw
    traceback to the wrapper, not a clean ``exit(2)``.
    """

    def test_non_utf8_file_surfaces_clean_error_not_traceback(self, brc_env, tmp_path, capsys):
        """**BUG**: ``--reason-file`` pointing at a non-UTF-8 file raises
        ``UnicodeDecodeError`` rather than exiting cleanly with rc=2.

        The wrapper bash sees a raw traceback on stderr instead of the
        helpful ``Error: failed to read --reason-file=...`` message the
        coder intended. The fix: catch ``(OSError, UnicodeDecodeError)``
        in ``_resolve_prose_arg``'s file-read branch.
        """
        binary_path = tmp_path / "binary.bin"
        binary_path.write_bytes(b"\xff\xfe\xc0\xc1 not valid utf-8")

        ns = _ack_ns(reason_file=str(binary_path))
        # Either the implementation cleanly exits with rc=2 (post-fix),
        # OR it raises UnicodeDecodeError (current behavior — the bug
        # this test surfaces). Both branches are exercised so the test
        # passes on either side of the fix while making the bug visible
        # in the failure rendering.
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            return_value={"ok": True, "signal": {}},
        ):
            try:
                rc = orch_cli.cmd_consensus_ack(ns)
            except UnicodeDecodeError as exc:
                pytest.fail(
                    "BUG: --reason-file with non-UTF-8 content raised "
                    f"UnicodeDecodeError instead of exiting cleanly: {exc}. "
                    "Fix: catch (OSError, UnicodeDecodeError) in "
                    "_resolve_prose_arg's file-read branch and emit "
                    "the same 'Error: failed to read ...' message."
                )
        # Post-fix path: clean exit code (2 for arg-validation failure).
        assert rc == 2, f"expected rc=2 on non-UTF-8 file, got rc={rc}"
        err = capsys.readouterr().err
        assert "--reason-file" in err
        assert "Traceback" not in err

    def test_missing_reason_file_path_surfaces_clean_error(self, brc_env, capsys):
        """``--reason-file /no/such/path`` returns rc=2 with a useful
        stderr message (no traceback, no SystemExit propagated past
        cmd_*)."""
        ns = _ack_ns(reason_file="/nonexistent/path/that/will/not/be/created.txt")
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            return_value={"ok": True, "signal": {}},
        ):
            rc = orch_cli.cmd_consensus_ack(ns)
        assert rc == 2, f"expected rc=2 on missing file, got rc={rc}"
        err = capsys.readouterr().err
        assert "--reason-file" in err
        assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Adversarial probes: --files-reviewed-file one-path-per-line edges
# ---------------------------------------------------------------------------


class TestFilesReviewedFileEdges:
    """One-path-per-line manifest semantics: blank lines and ``#``
    comments stripped; leading/trailing whitespace stripped; empty
    manifest collapses to ``[]`` which then fails the required-arg
    check at the cmd-handler layer.
    """

    def test_comments_only_manifest_yields_empty_list_then_required_error(
        self, brc_env, tmp_path, capsys
    ):
        """An all-comments manifest produces ``[]`` from the resolver,
        which then trips the required-arg check inside
        ``cmd_consensus_ack``. Surfaces a clean rc=2 with a stderr
        message (not silent ACK with empty files_reviewed)."""
        manifest = tmp_path / "all-comments.txt"
        manifest.write_text(
            "# only comments here\n"
            "# and more comments\n"
            "\n"  # blank
            "   \n"  # whitespace-only
            "# trailing\n",
            encoding="utf-8",
        )
        ns = _ack_ns(
            reason="dummy reason",
            files_reviewed=None,
            files_reviewed_file=str(manifest),
        )
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            return_value={"ok": True, "signal": {}},
        ):
            rc = orch_cli.cmd_consensus_ack(ns)
        assert rc == 2, f"expected rc=2 (required-arg failure) on empty manifest, got {rc}"
        err = capsys.readouterr().err
        assert "--files-reviewed" in err

    def test_paths_with_leading_whitespace_are_stripped(self, brc_env, tmp_path):
        """``  some/path.py`` becomes ``some/path.py``. Defended
        behavior: the wrapper may indent its manifest, and a leading-
        space typo should not produce a phantom file entry."""
        manifest = tmp_path / "indented.txt"
        manifest.write_text("    a.py\n\tb.py\nc.py\n", encoding="utf-8")
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        ns = _ack_ns(
            reason="r",
            files_reviewed=None,
            files_reviewed_file=str(manifest),
        )
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_consensus_ack(ns)
        assert rc == 0
        assert captured["files_reviewed"] == ["a.py", "b.py", "c.py"]

    def test_manifest_with_crlf_line_endings_handled(self, brc_env, tmp_path):
        """A Windows-line-ending manifest (CRLF) round-trips the same
        as LF — ``splitlines()`` handles both, but the strip pass also
        needs to strip the trailing ``\\r`` to avoid phantom entries
        like ``"a.py\\r"``. Pin the behavior."""
        manifest = tmp_path / "crlf.txt"
        manifest.write_bytes(b"a.py\r\n# comment\r\nb.py\r\n")
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        ns = _ack_ns(
            reason="r",
            files_reviewed=None,
            files_reviewed_file=str(manifest),
        )
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_consensus_ack(ns)
        assert rc == 0
        assert captured["files_reviewed"] == ["a.py", "b.py"], (
            f"CRLF manifest produced {captured['files_reviewed']!r}; "
            "trailing \\r must be stripped along with whitespace"
        )


# ---------------------------------------------------------------------------
# Adversarial probes: prose-arg empty / sentinel edges
# ---------------------------------------------------------------------------


class TestProseArgEmptyEdges:
    """Empty-channel semantics — the difference between ``argv=None``,
    ``argv=""``, ``stdin -`` with empty pipe, and ``--reason-file``
    pointing at an empty file."""

    def test_empty_string_argv_treated_as_missing(self, brc_env, capsys):
        """``--reason ""`` (explicit empty string) is NOT treated as a
        valid argv channel — it falls through to the required-arg
        check. Otherwise an empty wrapper-substituted variable would
        silently produce an empty-reason ACK.

        cmd_* returns rc=2 (the established orch_cli pattern for
        argument-validation failures); no SystemExit propagates."""
        ns = _ack_ns(reason="")
        rc = orch_cli.cmd_consensus_ack(ns)
        assert rc == 2
        err = capsys.readouterr().err
        assert "--reason" in err

    def test_stdin_sentinel_with_empty_pipe_returns_empty_string(self, brc_env, monkeypatch):
        """``--reason -`` with an empty stdin returns ``""`` verbatim
        and forwards it to the handler. Documents the intentional
        permissive behavior — the handler layer is the source of
        truth on min-length policy."""
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        ns = _ack_ns(reason="-")
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_consensus_ack(ns)
        assert rc == 0
        assert captured["reason"] == ""

    def test_reason_file_preserves_trailing_newline(self, brc_env, tmp_path):
        """``--reason-file PATH`` forwards file contents byte-equal,
        INCLUDING any trailing newline. Documenting this behavior so a
        future refactor that ``rstrip()``-s the value (changing the
        on-wire payload) shows up as a test failure rather than a
        silent diff in orchestrator-side reason rendering."""
        reason_path = tmp_path / "r.txt"
        reason_path.write_text("my reason\n", encoding="utf-8")
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        ns = _ack_ns(reason_file=str(reason_path))
        with patch(
            "egg_agent_tools.handlers.brc.brc_ack",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_consensus_ack(ns)
        assert rc == 0
        assert captured["reason"] == "my reason\n"


# ---------------------------------------------------------------------------
# Adversarial probes: brc resolve-obligation
# ---------------------------------------------------------------------------


class TestBrcResolveObligationAdversarial:
    def test_happy_path_stdout_message_format(self, brc_env, capsys):
        """Pins the human-readable line so a future format refactor
        (e.g. dropping the role from the prefix) surfaces as a test
        failure. The wrapper bash may key on this prefix in logs."""
        captured = {}

        def fake(req):
            captured.update(req)
            return {
                "ok": True,
                "role": "tester",
                "reviewer_role": req["reviewer_role"],
                "producer_role": req["producer_role"],
                "signal": {"signal_id": "obl-1"},
            }

        with patch(
            "egg_agent_tools.handlers.brc.brc_resolve_obligation",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_brc_resolve_obligation(_resolve_ns())
        assert rc == 0
        out = capsys.readouterr().out
        # Format: "Obligation resolved by <role>: reviewer=<rev> producer=<prod>"
        assert "Obligation resolved by tester" in out
        assert "reviewer=reviewer_contract" in out
        assert "producer=coder" in out

    def test_happy_path_with_commit_sha_renders_in_stdout(self, brc_env, capsys):
        """``--commit-sha`` round-trips into the stdout suffix."""
        with patch(
            "egg_agent_tools.handlers.brc.brc_resolve_obligation",
            return_value={"ok": True, "signal": {}},
        ):
            rc = orch_cli.cmd_brc_resolve_obligation(
                _resolve_ns(commit_sha="abc1234"),
            )
        assert rc == 0
        out = capsys.readouterr().out
        assert "commit=abc1234" in out

    def test_json_mode_outputs_valid_json(self, brc_env, capsys):
        """``--json`` mode prints valid JSON to stdout (the wrapper bash
        pipes this into ``jq``)."""
        with patch(
            "egg_agent_tools.handlers.brc.brc_resolve_obligation",
            return_value={
                "ok": True,
                "signal": {"signal_id": "obl-99", "timestamp": "2026-06-03T00:00:00"},
            },
        ):
            rc = orch_cli.cmd_brc_resolve_obligation(_resolve_ns(json=True))
        assert rc == 0
        decoded = json.loads(capsys.readouterr().out)
        assert decoded.get("signal_id") == "obl-99" or "signal_id" in decoded

    def test_handler_error_renders_cleanly_no_traceback(self, brc_env, capsys):
        """When the handler raises ``HandlerError`` (e.g. resolver ==
        producer rejection), the CLI exits non-zero and prints a clean
        message — no Python traceback on stderr or stdout."""
        with patch(
            "egg_agent_tools.handlers.brc.brc_resolve_obligation",
            side_effect=HandlerError("resolver_role must not equal producer_role"),
        ):
            rc = orch_cli.cmd_brc_resolve_obligation(_resolve_ns())
        assert rc != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out

    def test_note_argv_emits_deprecation_warning(self, brc_env):
        """``--note "some text"`` (argv) emits a DeprecationWarning
        just like the other prose-bearing args. Pins the consistency
        — a wrapper that uses argv ``--note`` is at the same #2741
        risk as argv ``--reason``."""
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "signal": {}}

        with warnings.catch_warnings(record=True) as warnlog:
            warnings.simplefilter("always")
            with patch(
                "egg_agent_tools.handlers.brc.brc_resolve_obligation",
                side_effect=fake,
            ):
                rc = orch_cli.cmd_brc_resolve_obligation(
                    _resolve_ns(note="resolved via cherry-pick"),
                )
        assert rc == 0
        assert captured.get("note") == "resolved via cherry-pick"
        depr = [w for w in warnlog if issubclass(w.category, DeprecationWarning)]
        assert depr and "--note" in str(depr[0].message), (
            "argv --note must emit DeprecationWarning like the other "
            "prose-bearing args; otherwise the wrapper bash quietly "
            "regresses to argv prose for --note"
        )


# ---------------------------------------------------------------------------
# Adversarial probes: brc read-peer-artifact
# ---------------------------------------------------------------------------


class TestBrcReadPeerArtifactAdversarial:
    def test_three_message_type_filters_thread_as_list(self, brc_env, capsys):
        """``--message-type ×3`` produces a 3-element list forwarded to
        the handler. Pins the ``action="append"`` semantics — a future
        refactor to ``nargs="+"`` would change the shape and break the
        wrapper's multi-filter use."""
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "phase": "implement", "items": [], "next_cursor": None}

        with patch(
            "egg_agent_tools.handlers.brc.brc_read_peer_artifact",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_brc_read_peer_artifact(
                _read_ns(
                    message_type=[
                        "CONSENSUS_PROPOSE",
                        "CONSENSUS_ACK",
                        "CONSENSUS_NACK",
                    ]
                ),
            )
        assert rc == 0
        assert captured["message_type"] == [
            "CONSENSUS_PROPOSE",
            "CONSENSUS_ACK",
            "CONSENSUS_NACK",
        ]

    def test_handler_validation_error_renders_cleanly(self, brc_env, capsys):
        """``--limit 0`` is rejected by the handler (limit must be > 0);
        the CLI must surface a clean rc != 0 with a stderr message,
        not a Python traceback. (argparse accepts any int; validation
        is delegated to the handler.)"""
        with patch(
            "egg_agent_tools.handlers.brc.brc_read_peer_artifact",
            side_effect=HandlerError("'limit' must be > 0"),
        ):
            rc = orch_cli.cmd_brc_read_peer_artifact(_read_ns(limit=0))
        assert rc != 0
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out

    def test_include_unattributed_default_true(self, brc_env):
        """When ``--no-include-unattributed`` is NOT passed, the
        request body sets ``include_unattributed=True``. Pins the
        default — a regression that flips it to False would silently
        narrow what reviewers see in slice-scoped implement phases."""
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "phase": "implement", "items": [], "next_cursor": None}

        with patch(
            "egg_agent_tools.handlers.brc.brc_read_peer_artifact",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_brc_read_peer_artifact(_read_ns())
        assert rc == 0
        assert captured["include_unattributed"] is True

    def test_no_pipeline_id_does_not_block_read(self, brc_env, monkeypatch):
        """The handler resolves the identifier server-side from
        ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` (caller overrides
        are ignored for cross-pipeline-read hardening). The CLI must
        NOT add a ``pipeline_id`` key to the request body that would
        be silently dropped — pin the absence so a refactor that
        re-introduces it surfaces."""
        captured = {}

        def fake(req):
            captured.update(req)
            return {"ok": True, "phase": "implement", "items": [], "next_cursor": None}

        with patch(
            "egg_agent_tools.handlers.brc.brc_read_peer_artifact",
            side_effect=fake,
        ):
            rc = orch_cli.cmd_brc_read_peer_artifact(_read_ns())
        assert rc == 0
        # The CLI deliberately omits pipeline_id — defended in the
        # cmd_brc_read_peer_artifact docstring.
        assert "pipeline_id" not in captured, (
            f"CLI must not forward pipeline_id (handler ignores it for "
            f"cross-pipeline hardening); got {captured!r}"
        )

    def test_invalid_phase_rejected_at_parse_time(self, brc_env, capsys):
        """``--phase bogus`` is rejected by argparse ``choices=`` before
        the handler runs. Documents the parse-time validation so a
        refactor that drops ``choices=`` surfaces."""
        parser = orch_cli.create_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["brc", "read-peer-artifact", "--phase", "not-a-real-phase"])
        # argparse exits 2 on bad choice.
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "phase" in err.lower()
