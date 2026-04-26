"""Tests for the ``egg-orch overseer file-issue`` CLI verb (issue #1962)."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from egg_lib.orch_cli import (
    _OVERSEER_BODY_MAX_BYTES,
    _OVERSEER_TITLE_MAX_CHARS,
    _OVERSEER_VALID_LABEL_PRIORITIES,
    cmd_overseer_file_issue,
    create_parser,
)

# ---------------------------------------------------------------------------
# argparse coverage
# ---------------------------------------------------------------------------


class TestOverseerFileIssueParser:
    def _argv(self, *extra: str) -> list[str]:
        # Minimum viable argv: anomaly-type, priority, agent-role,
        # anomaly-signature, issue-title-file, issue-body-file.
        return [
            "overseer",
            "file-issue",
            "--anomaly-type",
            "agent-loop",
            "--priority",
            "p1",
            "--agent-role",
            "coder",
            "--anomaly-signature",
            "abcdef0123456789",
            "--issue-title-file",
            "/tmp/title.txt",
            "--issue-body-file",
            "/tmp/body.md",
            *extra,
        ]

    def test_parser_accepts_minimum_args(self) -> None:
        parser = create_parser()
        ns = parser.parse_args(self._argv())
        assert ns.command == "overseer"
        assert ns.overseer_command == "file-issue"
        assert ns.anomaly_type == "agent-loop"
        assert ns.priority == "p1"
        assert ns.agent_role == "coder"
        assert ns.anomaly_signature == "abcdef0123456789"

    def test_dry_run_flag(self) -> None:
        parser = create_parser()
        ns = parser.parse_args(self._argv("--dry-run"))
        assert ns.dry_run is True

    @pytest.mark.parametrize("priority", list(_OVERSEER_VALID_LABEL_PRIORITIES))
    def test_priority_choices_accepted(self, priority: str) -> None:
        parser = create_parser()
        ns = parser.parse_args(
            [
                "overseer",
                "file-issue",
                "--anomaly-type",
                "x",
                "--priority",
                priority,
                "--agent-role",
                "r",
                "--anomaly-signature",
                "abcdef0123456789",
                "--issue-title-file",
                "t",
                "--issue-body-file",
                "b",
            ]
        )
        assert ns.priority == priority

    def test_invalid_priority_rejected(self) -> None:
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "overseer",
                    "file-issue",
                    "--anomaly-type",
                    "x",
                    "--priority",
                    "critical",
                    "--agent-role",
                    "r",
                    "--anomaly-signature",
                    "abcdef0123456789",
                    "--issue-title-file",
                    "t",
                    "--issue-body-file",
                    "b",
                ]
            )

    @pytest.mark.parametrize(
        "missing_flag",
        [
            "--anomaly-type",
            "--priority",
            "--agent-role",
            "--anomaly-signature",
            "--issue-title-file",
            "--issue-body-file",
        ],
    )
    def test_required_flags_enforced(self, missing_flag: str) -> None:
        # Drop the named flag (and its value) from the otherwise-valid argv.
        argv = self._argv()
        idx = argv.index(missing_flag)
        del argv[idx : idx + 2]
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


# ---------------------------------------------------------------------------
# cmd_overseer_file_issue behaviour
# ---------------------------------------------------------------------------


def _make_args(
    *,
    title: str = "[Pipeline Diagnostic] x - r [abcdef01]",
    body: str = "body",
    title_file: Path | None = None,
    body_file: Path | None = None,
    priority: str = "p1",
    dry_run: bool = False,
    json_flag: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        anomaly_type="agent-loop",
        priority=priority,
        agent_role="coder",
        anomaly_signature="abcdef0123456789",
        issue_title_file=str(title_file) if title_file else "/nonexistent/title",
        issue_body_file=str(body_file) if body_file else "/nonexistent/body",
        parent_alert_message_id="msg-1",
        dry_run=dry_run,
        json=json_flag,
    )


class TestOverseerFileIssueCommand:
    def test_missing_pipeline_repo_env(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("EGG_PIPELINE_REPO", raising=False)
        rc = cmd_overseer_file_issue(_make_args())
        assert rc == 2
        captured = capsys.readouterr()
        assert "EGG_PIPELINE_REPO" in captured.err

    def test_missing_title_file_returns_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        rc = cmd_overseer_file_issue(_make_args())
        assert rc == 2
        captured = capsys.readouterr()
        assert "issue-title-file" in captured.err

    def test_oversize_title_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("x" * (_OVERSEER_TITLE_MAX_CHARS + 1))
        body_file.write_text("body")
        rc = cmd_overseer_file_issue(_make_args(title_file=title_file, body_file=body_file))
        assert rc == 2
        captured = capsys.readouterr()
        assert "title exceeds" in captured.err

    def test_oversize_body_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok title")
        body_file.write_bytes(b"x" * (_OVERSEER_BODY_MAX_BYTES + 1))
        rc = cmd_overseer_file_issue(_make_args(title_file=title_file, body_file=body_file))
        assert rc == 2
        captured = capsys.readouterr()
        assert "body exceeds" in captured.err

    def test_dedup_match_skips_gh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok")
        body_file.write_text("body")

        # find_existing_issue returns a hit; gh subprocess MUST NOT run.
        with (
            patch(
                "egg_lib.overseer_issue_body.find_existing_issue",
                return_value=42,
            ),
            patch(
                "subprocess.run",
                side_effect=AssertionError("gh should not run on dedup hit"),
            ),
        ):
            rc = cmd_overseer_file_issue(
                _make_args(title_file=title_file, body_file=body_file, json_flag=True)
            )
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload == {
            "issue_number": 42,
            "filed": False,
            "dedup_match": 42,
        }

    def test_happy_path_calls_gh_and_writes_cache(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        monkeypatch.setenv("EGG_PIPELINE_ID", "issue-1")
        monkeypatch.setenv("EGG_PHASE", "implement")
        # Run from tmp_path so the JSONL cache is sandboxed.
        monkeypatch.chdir(tmp_path)

        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok title")
        body_file.write_text("ok body")

        # The sandbox `gh` wrapper at sandbox/scripts/gh proxies
        # `gh issue create` to the gateway and prints the issue URL on
        # stdout (no --json passthrough). The CLI verb parses the URL
        # to extract the issue number. Mirror that contract here.
        def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            assert argv[:3] == ["gh", "issue", "create"]
            # Title is passed inline (not --title-file) because the
            # wrapper does not recognise --title-file.
            assert "--title" in argv
            # --json must NOT be present — the wrapper drops it.
            assert "--json" not in argv
            assert "agent:overseer" in argv
            assert "p1" in argv
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="https://github.com/owner/repo/issues/777\n",
                stderr="",
            )

        with (
            patch(
                "egg_lib.overseer_issue_body.find_existing_issue",
                return_value=None,
            ),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            rc = cmd_overseer_file_issue(_make_args(title_file=title_file, body_file=body_file))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"issue_number": 777, "filed": True, "dedup_match": None}
        # The CLI persists the filing record to the local JSONL cache.
        cache = tmp_path / ".egg-state" / "oversight" / "filed-issues.jsonl"
        assert cache.exists()
        # First line is the header, second line is the record.
        lines = cache.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0])["_kind"] == "header"
        record = json.loads(lines[1])
        assert record["issue_number"] == 777
        assert record["anomaly_signature"] == "abcdef0123456789"
        assert record["hitl_outcome"] == "filed"

    def test_gh_failure_returns_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        monkeypatch.chdir(tmp_path)
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok")
        body_file.write_text("body")

        def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="rate limit")

        with (
            patch(
                "egg_lib.overseer_issue_body.find_existing_issue",
                return_value=None,
            ),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            rc = cmd_overseer_file_issue(_make_args(title_file=title_file, body_file=body_file))
        assert rc == 1
        assert "gh issue create exited 2" in capsys.readouterr().err

    def test_gh_unparseable_stdout_returns_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        monkeypatch.chdir(tmp_path)
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok")
        body_file.write_text("body")

        def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            # Neither a JSON object nor a recognisable issue URL.
            return subprocess.CompletedProcess(argv, 0, stdout="not-a-url", stderr="")

        with (
            patch(
                "egg_lib.overseer_issue_body.find_existing_issue",
                return_value=None,
            ),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            rc = cmd_overseer_file_issue(_make_args(title_file=title_file, body_file=body_file))
        assert rc == 1
        assert "did not contain an issue number" in capsys.readouterr().err

    def test_gh_subprocess_filenotfound_returns_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        monkeypatch.chdir(tmp_path)
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok")
        body_file.write_text("body")

        with (
            patch(
                "egg_lib.overseer_issue_body.find_existing_issue",
                return_value=None,
            ),
            patch("subprocess.run", side_effect=FileNotFoundError("gh")),
        ):
            rc = cmd_overseer_file_issue(_make_args(title_file=title_file, body_file=body_file))
        assert rc == 1
        assert "gh subprocess failed" in capsys.readouterr().err

    def test_dry_run_prints_argv_without_calling_gh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        monkeypatch.chdir(tmp_path)
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok")
        body_file.write_text("body content")

        with (
            patch(
                "egg_lib.overseer_issue_body.find_existing_issue",
                return_value=None,
            ),
            patch(
                "subprocess.run",
                side_effect=AssertionError("gh should not run in dry-run"),
            ),
        ):
            rc = cmd_overseer_file_issue(
                _make_args(
                    title_file=title_file,
                    body_file=body_file,
                    dry_run=True,
                )
            )
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["dry_run"] is True
        assert payload["filed"] is False
        argv = payload["argv"]
        assert argv[:3] == ["gh", "issue", "create"]
        assert "agent:overseer" in argv
        assert "p1" in argv

    def test_gh_url_stdout_is_parsed_for_issue_number(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Confirms the production path: gh prints the URL, the verb
        # extracts the number from the URL.
        monkeypatch.setenv("EGG_PIPELINE_REPO", "owner/repo")
        monkeypatch.chdir(tmp_path)
        title_file = tmp_path / "title"
        body_file = tmp_path / "body"
        title_file.write_text("ok")
        body_file.write_text("body")

        def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="https://github.com/owner/repo/issues/4242\n",
                stderr="",
            )

        with (
            patch(
                "egg_lib.overseer_issue_body.find_existing_issue",
                return_value=None,
            ),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            rc = cmd_overseer_file_issue(_make_args(title_file=title_file, body_file=body_file))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["issue_number"] == 4242
