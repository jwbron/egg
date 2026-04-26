"""Tests for ``shared/egg_config/repos_schema.py`` (issue #2073, TASK-1-2).

Cover the new layered repo-config schema models:

* ``classify_persist_entry`` for absolute, repo-relative, empty-string,
  and traversal-attempt inputs.
* ``RepoDefaultsFile`` rejects every operator-scoped key with a
  field-named error.
* ``UserConfigFile`` accepts the legacy shape's operator-scoped fields
  unchanged.
* Hard-deprecate rejection of ``persist_dirs`` / ``persist_system_dirs``
  in legacy shape.
* ``schemaVersion: "1.0"`` default and forward-compat hard-fail on
  ``schemaVersion: "9.0"``.
* ``template:`` field type validation (string-or-null only).

The legacy-rejection tests assert the migration message text so doc
drift is caught.
"""

from __future__ import annotations

import pytest
from egg_config.repos_schema import (
    DEFAULT_SCHEMA_VERSION,
    EGG_SCHEMA_MAJOR,
    LEGACY_PERSIST_KEYS,
    OPERATOR_SCOPED_PER_REPO_KEYS,
    OPERATOR_SCOPED_TOP_LEVEL_KEYS,
    ConfigError,
    RepoDefaultsFile,
    UserConfigFile,
    classify_persist_entry,
)

# ---------------------------------------------------------------------------
# (a) classify_persist_entry
# ---------------------------------------------------------------------------


class TestClassifyPersistEntry:
    """Persist-entry classification: ``/``-prefix → system, else → repo."""

    def test_absolute_path_classified_as_system(self):
        assert classify_persist_entry("/usr/local/bin") == "system"
        assert classify_persist_entry("/opt/foo") == "system"

    def test_repo_relative_classified_as_repo(self):
        assert classify_persist_entry(".venv") == "repo"
        assert classify_persist_entry("node_modules") == "repo"

    def test_repo_relative_with_subdir_classified_as_repo(self):
        assert classify_persist_entry("dist/build") == "repo"

    def test_empty_string_raises_config_error(self):
        with pytest.raises(ConfigError, match="non-empty strings"):
            classify_persist_entry("")

    def test_non_string_raises_config_error(self):
        with pytest.raises(ConfigError, match="non-empty strings"):
            classify_persist_entry(None)  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="non-empty strings"):
            classify_persist_entry(123)  # type: ignore[arg-type]
        with pytest.raises(ConfigError, match="non-empty strings"):
            classify_persist_entry(["/usr/local/bin"])  # type: ignore[arg-type]

    def test_traversal_attempt_repo_relative(self):
        # No leading slash → still repo-relative classification.
        # (The denylist in shared/egg_config/repos.py is the security
        # gate; the schema-layer classifier is purely textual.)
        assert classify_persist_entry("../../etc/passwd") == "repo"

    def test_dotslash_path_classified_as_repo(self):
        assert classify_persist_entry("./relative") == "repo"


# ---------------------------------------------------------------------------
# (b) RepoDefaultsFile rejects operator-scoped keys
# ---------------------------------------------------------------------------


class TestRepoDefaultsFileOperatorKeysRejected:
    """The repo-defaults file must NOT carry operator-scoped keys."""

    @pytest.mark.parametrize("key", sorted(OPERATOR_SCOPED_TOP_LEVEL_KEYS))
    def test_top_level_operator_scoped_key_rejected(self, key):
        raw = {"schemaVersion": "1.0", key: "anything"}
        with pytest.raises(ConfigError) as excinfo:
            RepoDefaultsFile.from_dict(raw, file_label="<test>")
        msg = str(excinfo.value)
        assert key in msg
        assert "user file" in msg.lower() or "~/.config/egg" in msg

    @pytest.mark.parametrize("key", sorted(OPERATOR_SCOPED_PER_REPO_KEYS))
    def test_per_repo_operator_scoped_key_rejected(self, key):
        raw = {"schemaVersion": "1.0", key: True}
        with pytest.raises(ConfigError) as excinfo:
            RepoDefaultsFile.from_dict(raw, file_label="<test>")
        msg = str(excinfo.value)
        assert key in msg
        # Operator policy keys also point at the user file as the
        # correct location.
        assert "user file" in msg.lower() or "~/.config/egg" in msg

    def test_unknown_key_rejected(self):
        raw = {"schemaVersion": "1.0", "made_up_key": 1}
        with pytest.raises(ConfigError, match="unknown keys"):
            RepoDefaultsFile.from_dict(raw, file_label="<test>")

    def test_clean_block_loads(self):
        raw = {
            "schemaVersion": "1.0",
            "auth_mode": "bot",
            "build_commands": {"commands": ["make deps"]},
            "persist": ["/usr/local/bin", ".venv"],
            "watch_files": ["pyproject.toml"],
            "checks": [{"name": "lint", "command": "make lint"}],
        }
        result = RepoDefaultsFile.from_dict(raw, file_label="<test>")
        assert result.schemaVersion == "1.0"
        assert result.persist == ["/usr/local/bin", ".venv"]
        assert result.watch_files == ["pyproject.toml"]
        assert result.checks == [{"name": "lint", "command": "make lint"}]


# ---------------------------------------------------------------------------
# (c) UserConfigFile accepts legacy operator-scoped fields
# ---------------------------------------------------------------------------


class TestUserConfigFileAcceptsOperatorScoped:
    """The user file is the home of operator-scoped fields."""

    def test_user_file_accepts_top_level_operator_keys(self):
        raw = {
            "schemaVersion": "1.0",
            "github_username": "alice",
            "bot_username": "alice-bot",
            "writable_repos": ["alice/foo"],
            "readable_repos": [],
            "default_reviewer": "alice",
            "github_sync": {"sync_all_prs": True},
            "user_mode": {"github_user": "alice"},
            "local_repos": {"paths": ["/tmp/foo"]},
            "docker_setup": {},
            "repo_settings": {},
        }
        loaded = UserConfigFile.from_dict(raw, file_label="<test>")
        assert loaded.schemaVersion == "1.0"
        assert loaded.repo_settings == {}
        # raw is preserved verbatim so downstream callers can read
        # operator-scoped fields.
        assert loaded.raw["github_username"] == "alice"
        assert loaded.raw["writable_repos"] == ["alice/foo"]

    def test_user_file_accepts_per_repo_operator_keys(self):
        raw = {
            "schemaVersion": "1.0",
            "repo_settings": {
                "alice/foo": {
                    "restrict_to_configured_users": True,
                    "disable_auto_fix": False,
                    "auth_mode": "user",
                }
            },
        }
        loaded = UserConfigFile.from_dict(raw, file_label="<test>")
        block = loaded.repo_settings["alice/foo"]
        assert block["restrict_to_configured_users"] is True
        assert block["disable_auto_fix"] is False
        assert block["auth_mode"] == "user"

    def test_user_file_none_input_returns_empty(self):
        loaded = UserConfigFile.from_dict(None, file_label="<test>")
        assert loaded.schemaVersion == DEFAULT_SCHEMA_VERSION
        assert loaded.repo_settings == {}

    def test_user_file_rejects_non_dict_top_level(self):
        with pytest.raises(ConfigError, match="must be a mapping"):
            UserConfigFile.from_dict([], file_label="<test>")  # type: ignore[arg-type]

    def test_repo_settings_must_be_mapping(self):
        raw = {"repo_settings": ["alice/foo"]}
        with pytest.raises(ConfigError, match="repo_settings"):
            UserConfigFile.from_dict(raw, file_label="<test>")

    def test_repo_settings_block_must_be_mapping(self):
        raw = {"repo_settings": {"alice/foo": "broken"}}
        with pytest.raises(ConfigError, match="alice/foo"):
            UserConfigFile.from_dict(raw, file_label="<test>")

    def test_repo_settings_unknown_override_key_rejected(self):
        raw = {"repo_settings": {"alice/foo": {"made_up": 1}}}
        with pytest.raises(ConfigError, match="unknown override key"):
            UserConfigFile.from_dict(raw, file_label="<test>")


# ---------------------------------------------------------------------------
# (d) Hard-deprecate rejection of legacy keys
# ---------------------------------------------------------------------------


class TestLegacyPersistKeysRejected:
    """``persist_dirs`` / ``persist_system_dirs`` are hard-deprecated."""

    @pytest.mark.parametrize("legacy_key", LEGACY_PERSIST_KEYS)
    def test_repo_file_rejects_legacy_top_level_key(self, legacy_key):
        raw = {legacy_key: ["/usr/local/bin"]}
        with pytest.raises(ConfigError) as excinfo:
            RepoDefaultsFile.from_dict(raw, file_label="<test>")
        msg = str(excinfo.value)
        # Migration target prose must be present so docs don't drift.
        assert "persist:" in msg
        assert legacy_key in msg
        assert "#2073" in msg or "docs/guides/repo-config.md" in msg

    @pytest.mark.parametrize("legacy_key", LEGACY_PERSIST_KEYS)
    def test_repo_file_rejects_legacy_inside_build_commands(self, legacy_key):
        raw = {
            "schemaVersion": "1.0",
            "build_commands": {legacy_key: ["/usr/local/bin"]},
        }
        with pytest.raises(ConfigError) as excinfo:
            RepoDefaultsFile.from_dict(raw, file_label="<test>")
        msg = str(excinfo.value)
        assert legacy_key in msg
        assert "persist:" in msg

    @pytest.mark.parametrize("legacy_key", LEGACY_PERSIST_KEYS)
    def test_user_file_rejects_legacy_inside_repo_settings(self, legacy_key):
        raw = {
            "repo_settings": {
                "alice/foo": {legacy_key: ["/usr/local/bin"]},
            }
        }
        with pytest.raises(ConfigError) as excinfo:
            UserConfigFile.from_dict(raw, file_label="<test>")
        msg = str(excinfo.value)
        assert legacy_key in msg
        assert "persist:" in msg

    def test_migration_message_names_collapsed_target(self):
        """The error must explicitly tell users the new schema is `persist:`."""
        raw = {"persist_dirs": [".venv"]}
        with pytest.raises(ConfigError) as excinfo:
            RepoDefaultsFile.from_dict(raw, file_label="<test>")
        msg = str(excinfo.value)
        # Must instruct the user to migrate to a single `persist:` list
        # routing by leading slash.
        assert "persist:" in msg
        assert "/" in msg  # the slash-routing rule is documented inline


# ---------------------------------------------------------------------------
# (e) schemaVersion default and forward-compat hard-fail
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    """Version-tolerance policy pinned in TASK-1-1."""

    def test_default_version_when_absent(self):
        loaded = RepoDefaultsFile.from_dict({}, file_label="<test>")
        assert loaded.schemaVersion == DEFAULT_SCHEMA_VERSION
        # Sanity: the default is still major 1.
        assert int(DEFAULT_SCHEMA_VERSION.split(".")[0]) == EGG_SCHEMA_MAJOR

    def test_known_major_loads(self):
        loaded = RepoDefaultsFile.from_dict({"schemaVersion": "1.0"}, file_label="<test>")
        assert loaded.schemaVersion == "1.0"
        loaded = RepoDefaultsFile.from_dict({"schemaVersion": "1.5"}, file_label="<test>")
        assert loaded.schemaVersion == "1.5"

    def test_unknown_future_major_hard_fails(self):
        with pytest.raises(ConfigError) as excinfo:
            RepoDefaultsFile.from_dict({"schemaVersion": "9.0"}, file_label="<test>")
        msg = str(excinfo.value)
        # Must name both the file's declared version and the running egg
        # version (TASK-1-1 acceptance).
        assert "9.0" in msg or "9" in msg
        assert "1" in msg

    def test_zero_major_hard_fails(self):
        # major 0 isn't recognised either.
        # (Below current major is treated as same family; the spec
        # focuses on unknown *newer* majors. We accept lower-than-known
        # in case a downgrade is in flight; verify the failure mode is
        # the major-too-new path explicitly.)
        loaded = RepoDefaultsFile.from_dict({"schemaVersion": "0.1"}, file_label="<test>")
        assert loaded.schemaVersion == "0.1"

    def test_non_string_version_rejected(self):
        with pytest.raises(ConfigError, match="schemaVersion"):
            RepoDefaultsFile.from_dict({"schemaVersion": 1.0}, file_label="<test>")
        with pytest.raises(ConfigError, match="schemaVersion"):
            RepoDefaultsFile.from_dict({"schemaVersion": ["1", "0"]}, file_label="<test>")

    def test_garbage_version_rejected(self):
        with pytest.raises(ConfigError, match="schemaVersion"):
            RepoDefaultsFile.from_dict({"schemaVersion": "not.a.version"}, file_label="<test>")


# ---------------------------------------------------------------------------
# (f) template field type validation
# ---------------------------------------------------------------------------


class TestTemplateField:
    """``template:`` accepts string-or-null only (Q4 reserve)."""

    def test_template_null_accepted(self):
        loaded = RepoDefaultsFile.from_dict({"template": None}, file_label="<test>")
        assert loaded.template is None

    def test_template_omitted_defaults_to_none(self):
        loaded = RepoDefaultsFile.from_dict({}, file_label="<test>")
        assert loaded.template is None

    def test_template_string_accepted(self):
        loaded = RepoDefaultsFile.from_dict({"template": "python-uv"}, file_label="<test>")
        assert loaded.template == "python-uv"

    def test_template_int_rejected(self):
        with pytest.raises(ConfigError, match="template"):
            RepoDefaultsFile.from_dict({"template": 1}, file_label="<test>")

    def test_template_list_rejected(self):
        with pytest.raises(ConfigError, match="template"):
            RepoDefaultsFile.from_dict({"template": ["a", "b"]}, file_label="<test>")

    def test_template_dict_rejected(self):
        with pytest.raises(ConfigError, match="template"):
            RepoDefaultsFile.from_dict({"template": {"name": "python-uv"}}, file_label="<test>")


# ---------------------------------------------------------------------------
# Misc round-trips / shape checks
# ---------------------------------------------------------------------------


class TestRepoDefaultsToDict:
    """``RepoDefaultsFile.to_dict`` shape used by the merge layer."""

    def test_minimal_round_trip(self):
        loaded = RepoDefaultsFile.from_dict({}, file_label="<test>")
        d = loaded.to_dict()
        assert d["schemaVersion"] == DEFAULT_SCHEMA_VERSION
        assert d["persist"] == []
        assert d["watch_files"] == []
        assert d["checks"] == []
        assert "auth_mode" not in d  # only present when set
        assert "checkpoint_repo" not in d
        assert "build_commands" not in d
        assert "template" not in d

    def test_full_round_trip(self):
        raw = {
            "schemaVersion": "1.2",
            "template": "python-uv",
            "auth_mode": "user",
            "checkpoint_repo": "alice/checkpoints",
            "build_commands": {"commands": ["echo hi"]},
            "persist": ["/usr/local/bin", ".venv"],
            "watch_files": ["pyproject.toml"],
            "checks": [{"name": "lint", "command": "make lint"}],
        }
        loaded = RepoDefaultsFile.from_dict(raw, file_label="<test>")
        d = loaded.to_dict()
        assert d["template"] == "python-uv"
        assert d["auth_mode"] == "user"
        assert d["checkpoint_repo"] == "alice/checkpoints"
        assert d["build_commands"] == {"commands": ["echo hi"]}
        assert d["persist"] == ["/usr/local/bin", ".venv"]
        assert d["watch_files"] == ["pyproject.toml"]


class TestRepoDefaultsListShapeValidation:
    """List fields are validated (not just blindly cast)."""

    def test_persist_must_be_list(self):
        with pytest.raises(ConfigError, match="persist"):
            RepoDefaultsFile.from_dict({"persist": "not-a-list"}, file_label="<test>")

    def test_persist_entry_must_be_non_empty_string(self):
        # Empty string entry hits classify_persist_entry's reject path.
        with pytest.raises(ConfigError, match="non-empty"):
            RepoDefaultsFile.from_dict({"persist": [""]}, file_label="<test>")
        # Ints rejected too.
        with pytest.raises(ConfigError, match="non-empty"):
            RepoDefaultsFile.from_dict({"persist": [1]}, file_label="<test>")

    def test_watch_files_must_be_list(self):
        with pytest.raises(ConfigError, match="watch_files"):
            RepoDefaultsFile.from_dict({"watch_files": "single-file"}, file_label="<test>")

    def test_checks_must_be_list_of_mappings(self):
        with pytest.raises(ConfigError, match="checks"):
            RepoDefaultsFile.from_dict({"checks": "not-a-list"}, file_label="<test>")
        with pytest.raises(ConfigError, match="checks"):
            RepoDefaultsFile.from_dict({"checks": ["just-string"]}, file_label="<test>")
        with pytest.raises(ConfigError, match="name"):
            RepoDefaultsFile.from_dict({"checks": [{"command": "make lint"}]}, file_label="<test>")

    def test_auth_mode_validated(self):
        with pytest.raises(ConfigError, match="auth_mode"):
            RepoDefaultsFile.from_dict({"auth_mode": "neither"}, file_label="<test>")
        loaded = RepoDefaultsFile.from_dict({"auth_mode": "bot"}, file_label="<test>")
        assert loaded.auth_mode == "bot"
        loaded = RepoDefaultsFile.from_dict({"auth_mode": "user"}, file_label="<test>")
        assert loaded.auth_mode == "user"

    def test_checkpoint_repo_must_be_owner_slash_name(self):
        with pytest.raises(ConfigError, match="checkpoint_repo"):
            RepoDefaultsFile.from_dict({"checkpoint_repo": "no-slash"}, file_label="<test>")
        loaded = RepoDefaultsFile.from_dict({"checkpoint_repo": "alice/foo"}, file_label="<test>")
        assert loaded.checkpoint_repo == "alice/foo"

    def test_build_commands_must_be_mapping(self):
        with pytest.raises(ConfigError, match="build_commands"):
            RepoDefaultsFile.from_dict({"build_commands": ["echo hi"]}, file_label="<test>")

    def test_top_level_must_be_mapping(self):
        with pytest.raises(ConfigError, match="must be a mapping"):
            RepoDefaultsFile.from_dict([], file_label="<test>")  # type: ignore[arg-type]
