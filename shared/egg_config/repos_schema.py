"""Schema models for the new layered repo-config format.

Issue #2073 introduces a unified user-facing repo-config schema with:

* a single ``persist:`` list (entries beginning with ``/`` are absolute
  system paths; everything else is repo-relative);
* a checked-in repo-defaults file at ``<repo>/.egg/repositories.yaml``
  carrying per-repo-scoped keys only; and
* an operator-side user file at ``~/.config/egg/repositories.yaml``
  carrying operator-scoped keys plus optional per-repo overrides.

This module exposes typed loaders for both shapes plus a
``classify_persist_entry`` helper that the merge layer and the manifest
classifier in :mod:`sandbox.egg_lib.docker` share.

Hard-deprecated keys (raised as :class:`ConfigError`):

* ``persist_dirs`` / ``persist_system_dirs`` — collapsed into ``persist:``.
* explicit-only ``watch_files`` at the top of the per-repo block (now
  inferred from manifests and overridable inside ``build_commands:``).

The version-tolerance policy is pinned here:

* ``schemaVersion`` defaults to ``"1.0"`` when absent (no warning).
* A known major (currently ``1``) loads.
* An unknown newer major hard-fails with a diagnostic naming both the
  file's declared version and the running egg version.

The internal ``manifest.json`` written into ``<config-dir>/repo-deps/``
is **independent** of this user-facing shape — the host-side classifier
in :mod:`sandbox.egg_lib.docker` produces the legacy two-list manifest
shape (``persist_dirs`` + ``persist_system_dirs``) so existing sandbox
images continue to work without a rebuild (architect C3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Egg's current schema major.  Bump this when the user-facing schema
# gains a backwards-incompatible change.
EGG_SCHEMA_MAJOR: int = 1

# Default ``schemaVersion`` injected when the file omits one.
DEFAULT_SCHEMA_VERSION: str = "1.0"

# Hard-deprecated keys: their presence is an error, not a warning.
LEGACY_PERSIST_KEYS: tuple[str, ...] = (
    "persist_dirs",
    "persist_system_dirs",
)

# Operator-scoped keys.  These belong in the user file only and are
# rejected when present in ``<repo>/.egg/repositories.yaml``.
OPERATOR_SCOPED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "github_username",
        "bot_username",
        "writable_repos",
        "readable_repos",
        "default_reviewer",
        "github_sync",
        "user_mode",
        "local_repos",
        "docker_setup",
        # ``repo_settings`` is the user-file map of partial overrides; it
        # belongs only in the user file because the repo file *is* the
        # per-repo block.
        "repo_settings",
    }
)

# Operator-scoped keys that live inside per-repo blocks but must not be
# expressed in a checked-in ``<repo>/.egg/repositories.yaml`` (the repo
# author should not be able to override the operator's policy).
OPERATOR_SCOPED_PER_REPO_KEYS: frozenset[str] = frozenset(
    {
        "restrict_to_configured_users",
        "disable_auto_fix",
    }
)

# Per-repo block keys that ARE allowed in a repo-defaults file.
ALLOWED_REPO_DEFAULTS_KEYS: frozenset[str] = frozenset(
    {
        "schemaVersion",
        "template",
        "build_commands",
        "persist",
        "watch_files",
        "checks",
        "auth_mode",
        "checkpoint_repo",
    }
)


class ConfigError(ValueError):
    """Raised when a repo / user config fails schema validation.

    Inherits from :class:`ValueError` so existing callers catching
    ``ValueError`` (the historical pattern in
    :mod:`config.repo_config`) keep working.
    """


def classify_persist_entry(entry: str) -> Literal["repo", "system"]:
    """Classify a unified ``persist:`` entry as repo-relative or system.

    Per HITL decision-3 the rule is purely textual: an entry beginning
    with ``/`` is a system absolute path; anything else is relative to
    the repo root.

    The returned label drives the host-side manifest classifier in
    :mod:`sandbox.egg_lib.docker`, which routes ``"repo"`` entries to the
    legacy ``persist_dirs`` field and ``"system"`` entries to
    ``persist_system_dirs``.

    Args:
        entry: A persist entry as it appears in
            ``<repo>/.egg/repositories.yaml`` or the user file.

    Returns:
        ``"repo"`` for repo-relative entries, ``"system"`` for absolute
        paths.

    Raises:
        ConfigError: If ``entry`` is not a non-empty string.
    """
    if not isinstance(entry, str) or not entry:
        raise ConfigError(
            "persist: entries must be non-empty strings; got "
            f"{entry!r}. Repo-relative paths (e.g. 'node_modules', "
            "'.venv') and absolute system paths (e.g. '/usr/local/bin') "
            "are both supported via leading-slash classification."
        )
    return "system" if entry.startswith("/") else "repo"


def _check_legacy_persist_keys(
    block: dict[str, Any], *, file_label: str
) -> None:
    """Reject the hard-deprecated ``persist_dirs`` / ``persist_system_dirs``.

    The migration target message is fixed prose so docs don't drift —
    `tests/shared/egg_config/test_repos_schema.py` asserts the exact text.
    """
    offenders = [k for k in LEGACY_PERSIST_KEYS if k in block]
    if not offenders:
        return
    raise ConfigError(
        f"{file_label}: legacy keys {offenders!r} are not supported "
        "since #2073. Merge them into a single 'persist:' list — "
        "entries beginning with '/' route to the absolute-path stage "
        "(replaces persist_system_dirs); other entries are repo-relative "
        "(replaces persist_dirs). See docs/guides/repo-config.md for "
        "the migration guide."
    )


def _check_schema_version(value: Any, *, file_label: str) -> str:
    """Enforce the version-tolerance policy on a ``schemaVersion`` value.

    Returns the normalised version string. Raises :class:`ConfigError`
    when the file declares an unknown future major.
    """
    if value is None:
        return DEFAULT_SCHEMA_VERSION
    if not isinstance(value, str):
        raise ConfigError(
            f"{file_label}: 'schemaVersion' must be a string like '1.0'; "
            f"got {type(value).__name__} ({value!r})."
        )
    head, _, _ = value.partition(".")
    try:
        major = int(head) if head else -1
    except ValueError as exc:
        raise ConfigError(
            f"{file_label}: 'schemaVersion' must be a string like '1.0'; "
            f"got {value!r}."
        ) from exc
    if major < 0:
        raise ConfigError(
            f"{file_label}: 'schemaVersion' must be a string like '1.0'; "
            f"got {value!r}."
        )
    if major > EGG_SCHEMA_MAJOR:
        raise ConfigError(
            f"{file_label}: schemaVersion {value!r} declares major "
            f"{major}, but this build of egg only understands major "
            f"{EGG_SCHEMA_MAJOR}. Upgrade egg or change the file's "
            "schemaVersion to a known major."
        )
    return value


def _check_template(value: Any, *, file_label: str) -> str | None:
    """Per-repo ``template:`` accepts only string-or-null."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ConfigError(
        f"{file_label}: 'template' must be a string or null; got "
        f"{type(value).__name__} ({value!r})."
    )


def _normalise_persist_list(
    value: Any, *, file_label: str
) -> list[str]:
    """Normalise a ``persist:`` list and validate every entry."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"{file_label}: 'persist' must be a list; got "
            f"{type(value).__name__}."
        )
    out: list[str] = []
    for entry in value:
        # ``classify_persist_entry`` raises on non-string / empty input.
        classify_persist_entry(entry)
        out.append(entry)
    return out


@dataclass
class RepoDefaultsFile:
    """The schema for ``<repo>/.egg/repositories.yaml``.

    A repo-defaults file declares per-repo-scoped configuration only.
    Operator-scoped keys are rejected outright with a diagnostic that
    points the author at the user file as the correct location.
    """

    schemaVersion: str = DEFAULT_SCHEMA_VERSION
    template: str | None = None
    build_commands: dict[str, Any] | None = None
    persist: list[str] = field(default_factory=list)
    watch_files: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    auth_mode: str | None = None
    checkpoint_repo: str | None = None

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], *, file_label: str = "<repo>/.egg/repositories.yaml"
    ) -> RepoDefaultsFile:
        """Validate ``raw`` and return a typed ``RepoDefaultsFile``.

        The reverse — turning a ``RepoDefaultsFile`` back into YAML — is
        not needed today (the merge layer in
        :mod:`shared.egg_config.repos` consumes the typed view directly).
        """
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{file_label}: top-level YAML must be a mapping; got "
                f"{type(raw).__name__}."
            )
        _check_legacy_persist_keys(raw, file_label=file_label)

        # Reject operator-scoped TOP-LEVEL keys (these belong only in
        # the user file).
        op_top = sorted(set(raw) & OPERATOR_SCOPED_TOP_LEVEL_KEYS)
        if op_top:
            raise ConfigError(
                f"{file_label}: operator-scoped keys {op_top!r} are not "
                "allowed in a checked-in repo-defaults file. Move them "
                "to your user file at ~/.config/egg/repositories.yaml."
            )

        # Reject operator-scoped per-repo keys
        # (restrict_to_configured_users / disable_auto_fix).  These are
        # operator policy and a feature branch shouldn't be able to flip
        # them.
        op_repo = sorted(set(raw) & OPERATOR_SCOPED_PER_REPO_KEYS)
        if op_repo:
            raise ConfigError(
                f"{file_label}: operator policy keys {op_repo!r} are not "
                "allowed in a checked-in repo-defaults file. Move them "
                "to your user file at ~/.config/egg/repositories.yaml."
            )

        # Catch unknown keys early so typos surface at write-time
        # instead of silently being dropped.
        unknown = sorted(
            k
            for k in raw
            if k not in ALLOWED_REPO_DEFAULTS_KEYS
        )
        if unknown:
            raise ConfigError(
                f"{file_label}: unknown keys {unknown!r}. Allowed keys "
                f"are {sorted(ALLOWED_REPO_DEFAULTS_KEYS)!r}."
            )

        return cls(
            schemaVersion=_check_schema_version(
                raw.get("schemaVersion"), file_label=file_label
            ),
            template=_check_template(
                raw.get("template"), file_label=file_label
            ),
            build_commands=_validate_build_commands(
                raw.get("build_commands"), file_label=file_label
            ),
            persist=_normalise_persist_list(
                raw.get("persist"), file_label=file_label
            ),
            watch_files=_validate_watch_files(
                raw.get("watch_files"), file_label=file_label
            ),
            checks=_validate_checks(
                raw.get("checks"), file_label=file_label
            ),
            auth_mode=_validate_auth_mode(
                raw.get("auth_mode"), file_label=file_label
            ),
            checkpoint_repo=_validate_checkpoint_repo(
                raw.get("checkpoint_repo"), file_label=file_label
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a dict representation suitable for merging."""
        out: dict[str, Any] = {
            "schemaVersion": self.schemaVersion,
            "persist": list(self.persist),
            "watch_files": list(self.watch_files),
            "checks": list(self.checks),
        }
        if self.template is not None:
            out["template"] = self.template
        if self.build_commands is not None:
            out["build_commands"] = dict(self.build_commands)
        if self.auth_mode is not None:
            out["auth_mode"] = self.auth_mode
        if self.checkpoint_repo is not None:
            out["checkpoint_repo"] = self.checkpoint_repo
        return out


@dataclass
class UserConfigFile:
    """The schema for ``~/.config/egg/repositories.yaml``.

    The user file is the historical operator-side config plus the
    optional ``repo_settings:`` map of partial per-repo overrides. Per
    HITL decision-12 / Q1 the legacy persist keys are dropped outright.
    """

    raw: dict[str, Any]
    schemaVersion: str = DEFAULT_SCHEMA_VERSION
    repo_settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any] | None,
        *,
        file_label: str = "~/.config/egg/repositories.yaml",
    ) -> UserConfigFile:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{file_label}: top-level YAML must be a mapping; got "
                f"{type(raw).__name__}."
            )

        # Top-level legacy persist keys are not valid here either —
        # historically they only appeared inside per-repo blocks, but
        # surface a clean error if someone tried to bubble them up.
        _check_legacy_persist_keys(raw, file_label=file_label)

        version = _check_schema_version(
            raw.get("schemaVersion"), file_label=file_label
        )

        repo_settings_raw = raw.get("repo_settings", {}) or {}
        if not isinstance(repo_settings_raw, dict):
            raise ConfigError(
                f"{file_label}: 'repo_settings' must be a mapping; got "
                f"{type(repo_settings_raw).__name__}."
            )

        # Each repo override is a partial of RepoDefaultsFile plus the
        # operator-scoped per-repo keys (restrict_to_configured_users /
        # disable_auto_fix), which ARE permitted here.
        normalised_repo_settings: dict[str, dict[str, Any]] = {}
        for repo_name, block in repo_settings_raw.items():
            if not isinstance(repo_name, str):
                raise ConfigError(
                    f"{file_label}: repo_settings keys must be strings; got "
                    f"{type(repo_name).__name__}."
                )
            if block is None:
                normalised_repo_settings[repo_name] = {}
                continue
            if not isinstance(block, dict):
                raise ConfigError(
                    f"{file_label}: repo_settings[{repo_name!r}] must be "
                    f"a mapping; got {type(block).__name__}."
                )
            block_label = f"{file_label} :: repo_settings[{repo_name!r}]"
            _check_legacy_persist_keys(block, file_label=block_label)
            # Operator-side overrides may use any persist path — the
            # denylist is a repo-file-only gate.
            normalised_repo_settings[repo_name] = _validate_user_per_repo_block(
                block, file_label=block_label
            )

        return cls(
            raw=raw,
            schemaVersion=version,
            repo_settings=normalised_repo_settings,
        )


def _validate_build_commands(
    value: Any, *, file_label: str
) -> dict[str, Any] | None:
    """Validate the ``build_commands:`` block as a mapping, if present."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(
            f"{file_label}: 'build_commands' must be a mapping; got "
            f"{type(value).__name__}."
        )
    # Per-key shape is loose here — the loader's classifier validates
    # ``persist:`` / ``watch_files:`` later.  We *do* reject the legacy
    # keys appearing inside the build_commands block as a safety net.
    _check_legacy_persist_keys(value, file_label=f"{file_label}.build_commands")
    return dict(value)


def _validate_watch_files(value: Any, *, file_label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"{file_label}: 'watch_files' must be a list; got "
            f"{type(value).__name__}."
        )
    return [str(v) for v in value]


def _validate_checks(value: Any, *, file_label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(
            f"{file_label}: 'checks' must be a list of "
            "{name, command} entries."
        )
    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ConfigError(
                f"{file_label}: each 'checks' entry must be a mapping; "
                f"got {type(entry).__name__}."
            )
        if "name" not in entry or "command" not in entry:
            raise ConfigError(
                f"{file_label}: each 'checks' entry must carry a 'name' "
                "and a 'command'."
            )
        out.append({"name": str(entry["name"]), "command": str(entry["command"])})
    return out


def _validate_auth_mode(value: Any, *, file_label: str) -> str | None:
    if value is None:
        return None
    if value not in ("bot", "user"):
        raise ConfigError(
            f"{file_label}: 'auth_mode' must be 'bot' or 'user'; got "
            f"{value!r}."
        )
    return str(value)


def _validate_checkpoint_repo(value: Any, *, file_label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(
            f"{file_label}: 'checkpoint_repo' must be a string of the "
            f"form 'owner/name'; got {type(value).__name__}."
        )
    if "/" not in value:
        raise ConfigError(
            f"{file_label}: 'checkpoint_repo' must be of the form "
            f"'owner/name'; got {value!r}."
        )
    return value


def _validate_user_per_repo_block(
    block: dict[str, Any], *, file_label: str
) -> dict[str, Any]:
    """Validate a per-repo override block as it appears in the user file.

    Operator-scoped per-repo keys (``restrict_to_configured_users`` /
    ``disable_auto_fix``) are permitted here. Persist-path denylists are
    NOT enforced — operators may persist whatever paths they want from
    their own machine.
    """
    out: dict[str, Any] = {}
    for key, value in block.items():
        if key in OPERATOR_SCOPED_PER_REPO_KEYS:
            # bool-coerce the policy flags but accept truthy values.
            out[key] = bool(value)
            continue
        if key == "schemaVersion":
            out[key] = _check_schema_version(value, file_label=file_label)
            continue
        if key == "template":
            out[key] = _check_template(value, file_label=file_label)
            continue
        if key == "build_commands":
            out[key] = _validate_build_commands(value, file_label=file_label)
            continue
        if key == "persist":
            out[key] = _normalise_persist_list(value, file_label=file_label)
            continue
        if key == "watch_files":
            out[key] = _validate_watch_files(value, file_label=file_label)
            continue
        if key == "checks":
            out[key] = _validate_checks(value, file_label=file_label)
            continue
        if key == "auth_mode":
            out[key] = _validate_auth_mode(value, file_label=file_label)
            continue
        if key == "checkpoint_repo":
            out[key] = _validate_checkpoint_repo(value, file_label=file_label)
            continue
        # Unknown keys: reject so typos surface immediately.
        raise ConfigError(
            f"{file_label}: unknown override key {key!r}."
        )
    return out


__all__ = [
    "ALLOWED_REPO_DEFAULTS_KEYS",
    "ConfigError",
    "DEFAULT_SCHEMA_VERSION",
    "EGG_SCHEMA_MAJOR",
    "LEGACY_PERSIST_KEYS",
    "OPERATOR_SCOPED_PER_REPO_KEYS",
    "OPERATOR_SCOPED_TOP_LEVEL_KEYS",
    "RepoDefaultsFile",
    "UserConfigFile",
    "classify_persist_entry",
]
