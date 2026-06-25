"""Direct unit tests for ``egg_agent.session`` (#3200, slice-6).

The session-state round-trip is the write-side substrate of the slice-8
resume-vs-reseed gate: ``__main__.py`` calls :func:`write_session_state` on
every run when ``$EGG_SESSION_STATE_FILE`` is set, and slice-8 will call
:func:`read_session_state` to decide whether to re-enter the prior Claude
session. ``test_client_resume.py`` covers the ``client.py`` plumbing only;
this module pins the module's *own* contract so a persistence-format
regression can't slip through silently before slice-8 builds on it.

The module's whole contract is "never raise — every failure cold-starts",
so the tests deliberately hammer the defensive branches: corrupt / empty /
non-object JSON, an unreadable file, a record with no usable ``session_id``,
the ``bool``-is-not-occupancy coercion rule, the blank-env-var-is-unset
rule, and the atomic write's OS-error degradation. The diagnostic
``logger.debug`` added for the anomalous read branches (and its silence on
the benign no-file / no-path branches) is asserted here too.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from egg_agent import session
from egg_agent.session import (
    SESSION_RESUME_ENV,
    SESSION_STATE_FILE_ENV,
    SessionState,
    read_session_state,
    resolve_session_state_path,
    session_resume_enabled,
    write_session_state,
)


@pytest.fixture(autouse=True)
def _clear_session_env(monkeypatch):
    """Keep every test on the default-OFF / no-path baseline unless it opts in."""
    monkeypatch.delenv(SESSION_RESUME_ENV, raising=False)
    monkeypatch.delenv(SESSION_STATE_FILE_ENV, raising=False)


# ── session_resume_enabled (opt-in, default OFF) ─────────────────────────────
class TestSessionResumeEnabled:
    def test_unset_is_disabled(self):
        assert session_resume_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "  on  "])
    def test_truthy_spellings_enable(self, monkeypatch, value):
        monkeypatch.setenv(SESSION_RESUME_ENV, value)
        assert session_resume_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "  "])
    def test_falsy_spellings_disable(self, monkeypatch, value):
        monkeypatch.setenv(SESSION_RESUME_ENV, value)
        assert session_resume_enabled() is False


# ── resolve_session_state_path ───────────────────────────────────────────────
class TestResolvePath:
    def test_none_when_unset(self):
        assert resolve_session_state_path() is None

    def test_explicit_arg_wins_over_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv(SESSION_STATE_FILE_ENV, str(tmp_path / "from-env.json"))
        resolved = resolve_session_state_path(tmp_path / "explicit.json")
        assert resolved == tmp_path / "explicit.json"

    def test_falls_back_to_env(self, monkeypatch, tmp_path):
        target = tmp_path / "from-env.json"
        monkeypatch.setenv(SESSION_STATE_FILE_ENV, str(target))
        assert resolve_session_state_path() == target

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_env_is_unset(self, monkeypatch, blank):
        """A blank env var must NOT point the round-trip at the cwd."""
        monkeypatch.setenv(SESSION_STATE_FILE_ENV, blank)
        assert resolve_session_state_path() is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_explicit_is_unset(self, blank):
        assert resolve_session_state_path(blank) is None

    def test_accepts_pathlike(self, tmp_path):
        target = tmp_path / "state.json"
        assert resolve_session_state_path(target) == target


# ── write / read round-trip ──────────────────────────────────────────────────
class TestRoundTrip:
    def test_write_then_read(self, tmp_path):
        path = tmp_path / "state.json"
        assert write_session_state("sess-abc", 4242, path=path) is True
        state = read_session_state(path)
        assert state == SessionState(session_id="sess-abc", window_occupancy=4242)

    def test_write_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "state.json"
        assert write_session_state("sess-1", 1, path=path) is True
        assert path.exists()
        assert read_session_state(path).session_id == "sess-1"

    def test_round_trip_via_env(self, monkeypatch, tmp_path):
        path = tmp_path / "state.json"
        monkeypatch.setenv(SESSION_STATE_FILE_ENV, str(path))
        assert write_session_state("sess-env") is True
        assert read_session_state().session_id == "sess-env"

    def test_none_occupancy_round_trips(self, tmp_path):
        path = tmp_path / "state.json"
        write_session_state("sess-x", None, path=path)
        assert read_session_state(path).window_occupancy is None

    def test_write_strips_session_id(self, tmp_path):
        path = tmp_path / "state.json"
        write_session_state("  sess-pad  ", 7, path=path)
        assert read_session_state(path).session_id == "sess-pad"

    def test_write_is_atomic_no_temp_left(self, tmp_path):
        """The temp file used for the atomic replace must not linger."""
        path = tmp_path / "state.json"
        write_session_state("sess-1", 1, path=path)
        leftovers = [p for p in tmp_path.iterdir() if p != path]
        assert leftovers == [], f"atomic write left stray files: {leftovers}"


# ── write — non-error no-ops return False ────────────────────────────────────
class TestWriteNoOps:
    def test_no_path_returns_false(self):
        assert write_session_state("sess-1", 1) is False

    @pytest.mark.parametrize("session_id", [None, "", "   "])
    def test_empty_session_id_returns_false_and_writes_nothing(self, tmp_path, session_id):
        path = tmp_path / "state.json"
        assert write_session_state(session_id, 1, path=path) is False
        assert not path.exists()

    def test_oserror_degrades_to_false(self, tmp_path):
        """A path whose parent is a regular file can't be written → False, no raise."""
        parent_as_file = tmp_path / "iam-a-file"
        parent_as_file.write_text("x", encoding="utf-8")
        path = parent_as_file / "state.json"
        assert write_session_state("sess-1", 1, path=path) is False


# ── read — every failure mode collapses to None ──────────────────────────────
class TestReadFallbacks:
    def test_no_path_returns_none(self):
        assert read_session_state() is None

    def test_missing_file_returns_none(self, tmp_path):
        assert read_session_state(tmp_path / "nope.json") is None

    def test_empty_file_returns_none(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("", encoding="utf-8")
        assert read_session_state(path) is None

    def test_whitespace_only_file_returns_none(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("   \n", encoding="utf-8")
        assert read_session_state(path) is None

    def test_malformed_json_returns_none(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert read_session_state(path) is None

    @pytest.mark.parametrize("payload", ["[1, 2, 3]", '"just a string"', "42", "null"])
    def test_non_object_payload_returns_none(self, tmp_path, payload):
        path = tmp_path / "state.json"
        path.write_text(payload, encoding="utf-8")
        assert read_session_state(path) is None

    @pytest.mark.parametrize(
        "record", [{}, {"session_id": ""}, {"session_id": "   "}, {"session_id": 123}]
    )
    def test_missing_or_unusable_session_id_returns_none(self, tmp_path, record):
        path = tmp_path / "state.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        assert read_session_state(path) is None

    def test_unreadable_file_returns_none(self, tmp_path, monkeypatch):
        """A non-FileNotFound OSError (e.g. permission) degrades to None."""
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"session_id": "sess-1"}), encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", boom)
        assert read_session_state(path) is None

    def test_extra_keys_are_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"session_id": "sess-1", "window_occupancy": 5, "future": "ok"}),
            encoding="utf-8",
        )
        assert read_session_state(path) == SessionState("sess-1", 5)


# ── _coerce_occupancy ────────────────────────────────────────────────────────
class TestCoerceOccupancy:
    @pytest.mark.parametrize("value,expected", [(0, 0), (5, 5), (-1, -1), (1_000_000, 1_000_000)])
    def test_ints_pass_through(self, value, expected):
        assert session._coerce_occupancy(value) == expected

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_not_occupancy(self, value):
        """``bool`` is an ``int`` subclass but must never be read as occupancy."""
        assert session._coerce_occupancy(value) is None

    @pytest.mark.parametrize("value", [None, "5", 3.5, [1], {"a": 1}])
    def test_non_int_becomes_none(self, value):
        assert session._coerce_occupancy(value) is None

    def test_bool_occupancy_round_trips_to_none(self, tmp_path):
        """A bool persisted under window_occupancy reads back as None, not 0/1."""
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"session_id": "s", "window_occupancy": True}), encoding="utf-8")
        assert read_session_state(path).window_occupancy is None


# ── diagnostic logging (finding #2): anomalous reads log, benign reads stay quiet ──
class TestReadDiagnostics:
    def _patch_logger(self, monkeypatch):
        fake = MagicMock()
        monkeypatch.setattr(session, "logger", fake)
        return fake

    def test_no_path_is_quiet(self, monkeypatch):
        fake = self._patch_logger(monkeypatch)
        read_session_state()
        fake.debug.assert_not_called()

    def test_missing_file_is_quiet(self, monkeypatch, tmp_path):
        fake = self._patch_logger(monkeypatch)
        read_session_state(tmp_path / "nope.json")
        fake.debug.assert_not_called()

    def test_malformed_json_logs_debug(self, monkeypatch, tmp_path):
        fake = self._patch_logger(monkeypatch)
        path = tmp_path / "state.json"
        path.write_text("{broken", encoding="utf-8")
        read_session_state(path)
        fake.debug.assert_called_once()

    def test_non_object_logs_debug(self, monkeypatch, tmp_path):
        fake = self._patch_logger(monkeypatch)
        path = tmp_path / "state.json"
        path.write_text("[1,2]", encoding="utf-8")
        read_session_state(path)
        fake.debug.assert_called_once()

    def test_unusable_session_id_logs_debug(self, monkeypatch, tmp_path):
        fake = self._patch_logger(monkeypatch)
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"session_id": ""}), encoding="utf-8")
        read_session_state(path)
        fake.debug.assert_called_once()

    def test_unreadable_logs_debug(self, monkeypatch, tmp_path):
        fake = self._patch_logger(monkeypatch)
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"session_id": "s"}), encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "read_text", boom)
        read_session_state(path)
        fake.debug.assert_called_once()

    def test_valid_read_is_quiet(self, monkeypatch, tmp_path):
        fake = self._patch_logger(monkeypatch)
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"session_id": "s", "window_occupancy": 1}), encoding="utf-8")
        assert read_session_state(path) == SessionState("s", 1)
        fake.debug.assert_not_called()


# ── __all__ surface is importable ────────────────────────────────────────────
def test_public_surface_is_complete():
    for name in session.__all__:
        assert hasattr(session, name), f"__all__ names missing attr: {name}"
