"""Tests for the cross-pod warm-resume session sync logic (#3278)."""

import json
import sys
from pathlib import Path

_sandbox_path = Path(__file__).parent.parent
if str(_sandbox_path) not in sys.path:
    sys.path.insert(0, str(_sandbox_path))

from egg_lib import session_state_sync as sync


class TestSlug:
    def test_matches_claude_code_algorithm(self):
        # Verified empirically against the installed build:
        # every non-alphanumeric char -> '-' (uppercase + '-' preserved).
        assert (
            sync.claude_project_slug("/home/egg/repos/My_Repo.v2-Test")
            == "-home-egg-repos-My-Repo-v2-Test"
        )

    def test_is_absolute(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # A relative path is resolved to absolute before slugging.
        assert sync.claude_project_slug("sub/dir").startswith("-")

    def test_transcript_path_shape(self):
        p = sync.transcript_path("/cfg", "/home/egg/repos/webapp", "sid-1")
        assert p == Path("/cfg/projects/-home-egg-repos-webapp/sid-1.jsonl")


class TestSafeSessionId:
    def test_accepts_uuid_and_token_shapes(self):
        assert sync.is_safe_session_id("550e8400-e29b-41d4-a716-446655440000")
        assert sync.is_safe_session_id("sid_1")

    def test_rejects_traversal_and_separators(self):
        assert not sync.is_safe_session_id("")
        assert not sync.is_safe_session_id("..")
        assert not sync.is_safe_session_id("../../etc/passwd")
        assert not sync.is_safe_session_id("a/b")
        assert not sync.is_safe_session_id(".hidden")

    def test_transcript_path_raises_on_unsafe_id(self):
        import pytest

        with pytest.raises(ValueError):
            sync.transcript_path("/cfg", "/repo", "../escape")

    def test_pull_rejects_unsafe_id_without_escaping(self, tmp_path):
        cfg = tmp_path / "cfg"
        ssf = tmp_path / "state.json"
        # A traversal-bearing session_id must not write anything (no escape).
        assert (
            sync.write_pulled_state(
                {"session_id": "../evil", "transcript": "x"},
                repo_path="/repo",
                config_dir=cfg,
                session_state_file=str(ssf),
            )
            is False
        )
        assert not ssf.exists()
        assert not (cfg / "projects").exists()

    def test_push_rejects_unsafe_id(self, tmp_path):
        ssf = tmp_path / "state.json"
        ssf.write_text(json.dumps({"session_id": "../evil", "window_occupancy": 7}))
        assert (
            sync.read_state_for_push(
                repo_path="/repo", config_dir=tmp_path / "cfg", session_state_file=str(ssf)
            )
            is None
        )


class TestResolvers:
    def test_config_dir_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/env")
        assert sync.resolve_config_dir("/explicit") == Path("/explicit")

    def test_config_dir_env_then_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/env")
        assert sync.resolve_config_dir() == Path("/env")
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        assert sync.resolve_config_dir() == Path.home() / ".claude"

    def test_repo_path_env_then_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EGG_REPO_PATH", "/repo")
        assert sync.resolve_repo_path() == "/repo"
        monkeypatch.delenv("EGG_REPO_PATH")
        monkeypatch.chdir(tmp_path)
        assert sync.resolve_repo_path() == str(tmp_path)


class TestPullWrite:
    def test_writes_pointer_and_transcript(self, tmp_path):
        cfg = tmp_path / "cfg"
        ssf = tmp_path / "state.json"
        repo = "/home/egg/repos/webapp"
        resumed = sync.write_pulled_state(
            {"session_id": "sid-1", "window_occupancy": 42, "transcript": '{"l":1}\n'},
            repo_path=repo,
            config_dir=cfg,
            session_state_file=str(ssf),
        )
        assert resumed is True
        # Pointer matches the egg_agent.session format.
        pointer = json.loads(ssf.read_text())
        assert pointer == {"session_id": "sid-1", "window_occupancy": 42}
        # Transcript at the exact path --resume reads.
        tpath = sync.transcript_path(cfg, repo, "sid-1")
        assert tpath.read_text() == '{"l":1}\n'

    def test_pointer_only_returns_false_no_transcript_file(self, tmp_path):
        cfg = tmp_path / "cfg"
        ssf = tmp_path / "state.json"
        resumed = sync.write_pulled_state(
            {"session_id": "sid-1", "window_occupancy": 42, "transcript": None},
            repo_path="/repo",
            config_dir=cfg,
            session_state_file=str(ssf),
        )
        assert resumed is False
        assert json.loads(ssf.read_text())["session_id"] == "sid-1"
        assert not (cfg / "projects").exists()

    def test_no_session_id_writes_nothing(self, tmp_path):
        ssf = tmp_path / "state.json"
        assert (
            sync.write_pulled_state(
                {"session_id": "", "transcript": "x"},
                repo_path="/repo",
                config_dir=tmp_path / "cfg",
                session_state_file=str(ssf),
            )
            is False
        )
        assert not ssf.exists()


class TestPushRead:
    def test_reads_pointer_and_transcript(self, tmp_path):
        cfg = tmp_path / "cfg"
        ssf = tmp_path / "state.json"
        repo = "/home/egg/repos/webapp"
        ssf.write_text(json.dumps({"session_id": "sid-1", "window_occupancy": 7}))
        tpath = sync.transcript_path(cfg, repo, "sid-1")
        tpath.parent.mkdir(parents=True)
        tpath.write_text('{"l":1}\n')

        body = sync.read_state_for_push(repo_path=repo, config_dir=cfg, session_state_file=str(ssf))
        assert body == {
            "session_id": "sid-1",
            "window_occupancy": 7,
            "transcript": '{"l":1}\n',
        }

    def test_missing_transcript_yields_pointer_only_body(self, tmp_path):
        ssf = tmp_path / "state.json"
        ssf.write_text(json.dumps({"session_id": "sid-1", "window_occupancy": 7}))
        body = sync.read_state_for_push(
            repo_path="/repo", config_dir=tmp_path / "cfg", session_state_file=str(ssf)
        )
        assert body is not None
        assert body["session_id"] == "sid-1"
        assert body["transcript"] is None

    def test_no_pointer_file_returns_none(self, tmp_path):
        assert (
            sync.read_state_for_push(
                repo_path="/repo",
                config_dir=tmp_path / "cfg",
                session_state_file=str(tmp_path / "missing.json"),
            )
            is None
        )

    def test_pointer_without_session_id_returns_none(self, tmp_path):
        ssf = tmp_path / "state.json"
        ssf.write_text(json.dumps({"window_occupancy": 7}))
        assert (
            sync.read_state_for_push(
                repo_path="/repo", config_dir=tmp_path / "cfg", session_state_file=str(ssf)
            )
            is None
        )


class TestRoundTrip:
    def test_pull_then_push_is_stable(self, tmp_path):
        """A pulled record read back for push reproduces the same body."""
        cfg = tmp_path / "cfg"
        ssf = tmp_path / "state.json"
        repo = "/home/egg/repos/webapp"
        record = {"session_id": "sid-1", "window_occupancy": 42, "transcript": '{"l":1}\n'}
        sync.write_pulled_state(record, repo_path=repo, config_dir=cfg, session_state_file=str(ssf))
        body = sync.read_state_for_push(repo_path=repo, config_dir=cfg, session_state_file=str(ssf))
        assert body == record
