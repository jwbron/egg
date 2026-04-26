"""Two-tier repo-config validator (issue #2073, phase 4).

Surfaces the known onboarding footguns at config-write time so they
don't lurk inside a sandbox image for weeks. Used by:

* ``scripts/validate-config.py --repo-config <path>`` (the
  ``egg validate-config`` CLI), and
* ``mcp__egg__validate_repo_config`` (the agent-facing MCP tool).

Errors block (return code 1); warnings advise (informational only).
The tier classification follows HITL decision-11.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .repos import (
    _enforce_repo_persist_denylist,
    load_merged_repo_config,
)
from .repos_schema import (
    OPERATOR_SCOPED_PER_REPO_KEYS,
    OPERATOR_SCOPED_TOP_LEVEL_KEYS,
    ConfigError,
    RepoDefaultsFile,
    UserConfigFile,
    classify_persist_entry,
)


@dataclass
class ValidationResult:
    """Two-tier validation result.

    ``errors`` list is non-empty iff the caller should fail (CLI exits
    1). ``warnings`` is informational.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


# Regex used to spot install commands that drop binaries into a system
# path. Heuristic — false positives are accepted as warnings, missing
# coverage is the bigger concern.
_INSTALL_TO_SYSTEM_PATH_RE = re.compile(
    r"(?:--prefix[= ]|UV_INSTALL_DIR=|"
    r"-C\s+|--install-dir[= ]|-o\s+)"
    r"(/usr/local/[A-Za-z0-9._/-]+|/opt/[A-Za-z0-9._/-]+)"
)
_TAR_TO_USR_LOCAL_RE = re.compile(r"tar\b[^|;&]*-C\s+(/usr/local/?\S*)")
_PIP_INSTALL_NEEDS_SOURCE_RE = re.compile(
    r"\bpip\s+install\b[^;|&]*\b-e\s+\.|\bpip\s+install\b[^;|&]*\bsetup\.py"
)
_UV_SYNC_NEEDS_SOURCE_RE = re.compile(
    r"\buv\s+sync\b(?![^;|&]*--no-install-project)"
)
_NPM_INSTALL_RE = re.compile(r"\bnpm\s+(?:ci|install|i)\b")
_CURL_OR_WGET_RE = re.compile(r"\b(?:curl|wget)\b")


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file. Raises :class:`ConfigError` on failure."""
    try:
        with path.open() as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Failed to read YAML at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: top-level YAML must be a mapping; got "
            f"{type(data).__name__}."
        )
    return data


def _is_private_mode_active() -> bool:
    """Return True if egg's network-locked private mode is in effect.

    Reads the runtime flag from ``EGG_PRIVATE_MODE`` / ``PRIVATE_MODE``
    env vars (the launcher and gateway both publish these) and
    ``EGG_NETWORK_MODE``. The condition is the network-mode flag *only*
    — independent of ``restrict_to_configured_users`` per NACK
    non-blocking.
    """
    for var in ("EGG_PRIVATE_MODE", "PRIVATE_MODE"):
        val = os.environ.get(var, "").strip().lower()
        if val in ("true", "1", "yes"):
            return True
    network_mode = os.environ.get("EGG_NETWORK_MODE", "").strip().lower()
    return network_mode == "private"


def _command_installs_to_system_path(command: str) -> str | None:
    """Best-effort: extract the absolute install path a command writes to."""
    m = _INSTALL_TO_SYSTEM_PATH_RE.search(command)
    if m:
        return m.group(1)
    m = _TAR_TO_USR_LOCAL_RE.search(command)
    if m:
        return m.group(1)
    return None


def _check_install_paths_persisted(
    *,
    repo_label: str,
    commands: list[str],
    persist_entries: list[str],
    result: ValidationResult,
) -> None:
    """Check (a): install commands target a path the persist list covers.

    Heuristic: if the command appears to install something at
    ``/usr/local/<x>`` or ``/opt/<x>`` and no ``persist:`` entry
    prefix-matches that path, raise an error (this is the #2065 trap).
    """
    for cmd in commands:
        target = _command_installs_to_system_path(cmd)
        if not target:
            continue
        covered = any(
            classify_persist_entry(entry) == "system"
            and target.startswith(entry.rstrip("/") + "/")
            or target.rstrip("/") == entry.rstrip("/")
            for entry in persist_entries
        )
        if not covered:
            result.add_error(
                f"{repo_label}: build_commands install to {target!r} but "
                "no `persist:` entry covers it. Add the install dir "
                "(or its parent) to `persist:` so the binaries are "
                "carried into the runtime image. See #2065."
            )


def _check_build_context_needs_source(
    *,
    repo_label: str,
    commands: list[str],
    watch_files: list[str],
    result: ValidationResult,
) -> None:
    """Check (b): #2087 — build commands need source the build context lacks.

    The build context is constructed from ``watch_files`` only. If the
    user runs ``uv sync`` (without ``--no-install-project``),
    ``pip install -e .``, or other commands that need the source tree,
    the build will fail at runtime. Surfaced as a warning with a
    ``--no-install-project`` hint.
    """
    has_source_files = any(
        not f.endswith((".lock", ".toml", ".json", ".txt", ".yaml", ".yml")) and "/" not in f
        for f in watch_files
    ) or any(f.startswith("src/") or f.startswith("egg_") for f in watch_files)

    for cmd in commands:
        if has_source_files:
            continue
        if _UV_SYNC_NEEDS_SOURCE_RE.search(cmd):
            result.add_warning(
                f"{repo_label}: command {cmd!r} runs `uv sync` against "
                "a watch-files-only build context. Add "
                "`--no-install-project` (or wrap it in a Makefile target "
                "like `make sandbox-deps`) so only third-party deps are "
                "installed — the local project doesn't need to build "
                "for ruff/pytest/etc. to work in the sandbox. See #2087."
            )
        elif _PIP_INSTALL_NEEDS_SOURCE_RE.search(cmd):
            result.add_warning(
                f"{repo_label}: command {cmd!r} installs from local "
                "source but the build context only carries watch_files. "
                "Use `pip install -r requirements.txt` (no -e .) or "
                "include the source files in watch_files."
            )


def _check_makefile_targets(
    *,
    repo_label: str,
    repo_path: Path,
    checks: list[dict[str, Any]],
    result: ValidationResult,
) -> None:
    """Check (c): ``checks.command`` references a real Makefile target."""
    makefile = repo_path / "Makefile"
    if not makefile.is_file():
        return
    try:
        content = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    target_re = re.compile(r"^([A-Za-z0-9_./-]+):", re.MULTILINE)
    targets = set(target_re.findall(content))

    for entry in checks:
        cmd = str(entry.get("command", ""))
        # Match `make foo` and `make foo bar` invocations.
        m = re.match(r"^make\s+([A-Za-z0-9_./-]+)", cmd)
        if not m:
            continue
        target = m.group(1)
        if target not in targets:
            result.add_error(
                f"{repo_label}: checks entry {entry.get('name', '?')!r} "
                f"runs `make {target}` but the target {target!r} is not "
                f"defined in {makefile}. Define it or correct the "
                "command."
            )


def _check_local_repos_paths(
    *,
    user_dict: dict[str, Any],
    user_path: Path | None,
    result: ValidationResult,
) -> None:
    """Check (e): every ``local_repos.paths`` entry exists on disk."""
    local_repos = user_dict.get("local_repos") or {}
    if not isinstance(local_repos, dict):
        return
    paths = local_repos.get("paths") or []
    if not isinstance(paths, list):
        return
    label = str(user_path) if user_path else "<user file>"
    for entry in paths:
        if not isinstance(entry, str) or not entry:
            continue
        candidate = Path(entry).expanduser()
        if not candidate.exists():
            result.add_error(
                f"{label}: local_repos.paths entry {entry!r} does not "
                "exist on disk. Remove the stale entry or fix the "
                "path."
            )


def _check_repo_file_operator_keys(
    *,
    repo_path: Path,
    repo_dict: dict[str, Any],
    result: ValidationResult,
) -> None:
    """Check (h): repo file rejects operator-scoped keys.

    The schema layer raises :class:`ConfigError` on these — but the
    validator surfaces them cleanly for the CLI/MCP output rather than
    a stack trace.
    """
    label = str(repo_path)
    op_top = sorted(set(repo_dict) & OPERATOR_SCOPED_TOP_LEVEL_KEYS)
    if op_top:
        result.add_error(
            f"{label}: operator-scoped top-level keys {op_top!r} are "
            "not allowed in a checked-in repo-defaults file. Move them "
            "to ~/.config/egg/repositories.yaml."
        )
    op_repo = sorted(set(repo_dict) & OPERATOR_SCOPED_PER_REPO_KEYS)
    if op_repo:
        result.add_error(
            f"{label}: operator policy keys {op_repo!r} are not "
            "allowed in a checked-in repo-defaults file. Move them to "
            "~/.config/egg/repositories.yaml."
        )


def _check_repo_persist_denylist(
    *,
    repo_path: Path,
    persist: list[str],
    result: ValidationResult,
) -> None:
    """Check: repo-file persist denylist (NACK-5)."""
    try:
        _enforce_repo_persist_denylist(persist, repo_label=str(repo_path))
    except ConfigError as exc:
        result.add_error(str(exc))


def _check_auth_mode_user_token(
    *,
    repo_label: str,
    auth_mode: str | None,
    result: ValidationResult,
) -> None:
    """Check (i): ``auth_mode: user`` without ``GITHUB_USER_TOKEN`` (warning)."""
    if auth_mode != "user":
        return
    token = os.environ.get("GITHUB_USER_TOKEN", "").strip()
    if token:
        return
    result.add_warning(
        f"{repo_label}: auth_mode is 'user' but no GITHUB_USER_TOKEN "
        "is configured at validate-time. Runtime injection paths exist "
        "(env var, secrets manager, CI variable) — set the token "
        "before running egg if you haven't already."
    )


def _check_persist_empty_dir(
    *,
    repo_label: str,
    persist: list[str],
    repo_path: Path | None,
    result: ValidationResult,
) -> None:
    """Check (j): persist of an empty directory (warning)."""
    if repo_path is None:
        return
    for entry in persist:
        if classify_persist_entry(entry) == "system":
            continue
        target = repo_path / entry
        if target.is_dir():
            try:
                if not any(target.iterdir()):
                    result.add_warning(
                        f"{repo_label}: persist entry {entry!r} points "
                        "at an empty directory. Either populate it "
                        "during build_commands or drop the entry."
                    )
            except OSError:
                pass


def _check_curl_in_private_mode(
    *,
    repo_label: str,
    commands: list[str],
    result: ValidationResult,
) -> None:
    """Check (k): curl/wget in build_commands while private mode is active.

    Network-mode condition only — decoupled from
    ``restrict_to_configured_users`` per NACK non-blocking.
    """
    if not _is_private_mode_active():
        return
    for cmd in commands:
        if _CURL_OR_WGET_RE.search(cmd):
            result.add_warning(
                f"{repo_label}: build_commands invokes curl/wget under "
                "egg's network-locked private mode. The Docker build "
                "stage may have outbound access, but a runtime "
                "container in private mode will not — verify the "
                "downloaded binaries persist into the image (see #2065)."
            )


def _check_writable_repos_have_settings(
    *,
    user_dict: dict[str, Any],
    user_path: Path | None,
    result: ValidationResult,
) -> None:
    """Check (f): every ``writable_repos`` entry has a corresponding settings block.

    A ``writable_repos`` entry without ``repo_settings[<name>]`` is a
    dangling reference — the auto-fix logic will run with empty
    defaults. Surface as a warning so the operator can decide to add a
    block or accept the dangling reference.
    """
    label = str(user_path) if user_path else "<user file>"
    writable = user_dict.get("writable_repos") or []
    if not isinstance(writable, list):
        return
    repo_settings = user_dict.get("repo_settings") or {}
    if not isinstance(repo_settings, dict):
        repo_settings = {}
    settings_lower = {str(k).lower() for k in repo_settings}
    for entry in writable:
        if not isinstance(entry, str):
            continue
        if entry.lower() not in settings_lower:
            result.add_warning(
                f"{label}: writable_repos entry {entry!r} has no "
                "matching repo_settings block. Auto-fix will run with "
                "no per-repo overrides."
            )


def _check_checkpoint_repo_format(
    *,
    repo_label: str,
    checkpoint_repo: str | None,
    result: ValidationResult,
) -> None:
    """Check (g): ``checkpoint_repo`` is well-formed.

    Reachability is best-effort and would require a network call, so we
    only validate the ``owner/name`` format here.
    """
    if checkpoint_repo is None:
        return
    if "/" not in checkpoint_repo or checkpoint_repo.startswith("/"):
        result.add_warning(
            f"{repo_label}: checkpoint_repo {checkpoint_repo!r} is not "
            "a well-formed 'owner/name'. Checkpoint pushes will fail."
        )


def _check_watch_files_match_commands(
    *,
    repo_label: str,
    watch_files: list[str],
    commands: list[str],
    result: ValidationResult,
) -> None:
    """Check (d): ``watch_files`` matches what ``build_commands`` actually use."""
    cmd_blob = " ".join(commands)
    has_pip = "pip install" in cmd_blob
    has_npm = bool(_NPM_INSTALL_RE.search(cmd_blob))
    has_go = "go mod" in cmd_blob or "go build" in cmd_blob
    has_uv = "uv sync" in cmd_blob or "uv pip" in cmd_blob

    watch_lower = {f.lower() for f in watch_files}

    expected_for_signal: list[tuple[str, set[str]]] = []
    if has_pip:
        expected_for_signal.append(
            (
                "pip install",
                {"requirements.txt", "requirements-dev.txt", "pyproject.toml"},
            )
        )
    if has_uv:
        expected_for_signal.append(
            ("uv", {"pyproject.toml", "uv.lock"})
        )
    if has_npm:
        expected_for_signal.append(
            ("npm", {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"})
        )
    if has_go:
        expected_for_signal.append(("go", {"go.mod", "go.sum"}))

    for signal_name, expected in expected_for_signal:
        if not (watch_lower & expected):
            result.add_warning(
                f"{repo_label}: build_commands include {signal_name} "
                f"but watch_files lacks any of {sorted(expected)!r}. "
                "Add the manifest to watch_files so Docker layer "
                "caching tracks it correctly."
            )


def validate_repo_config(
    checkout: Path | None,
    user_path: Path | None = None,
) -> ValidationResult:
    """Run the full two-tier validator (issue #2073, TASK-4-1).

    Args:
        checkout: Path to the working checkout whose
            ``.egg/repositories.yaml`` should be auto-discovered.
        user_path: Optional explicit user-file location. Falls back to
            the historical search paths.

    Returns:
        :class:`ValidationResult` with errors and warnings populated.
    """
    result = ValidationResult()

    # Pre-load both files raw so we can surface schema errors as
    # validator output rather than stack traces.
    user_dict: dict[str, Any] = {}
    user_resolved: Path | None = user_path
    if user_path is not None and user_path.exists():
        try:
            user_dict = _read_yaml(user_path)
        except ConfigError as exc:
            result.add_error(str(exc))
            return result

    repo_dict: dict[str, Any] = {}
    repo_path: Path | None = None
    if checkout is not None:
        repo_path = Path(checkout) / ".egg" / "repositories.yaml"
        if repo_path.exists():
            try:
                repo_dict = _read_yaml(repo_path)
            except ConfigError as exc:
                result.add_error(str(exc))
                return result
        else:
            repo_path = None

    # Schema validation — surface schema errors via the validator.
    if repo_path is not None:
        _check_repo_file_operator_keys(
            repo_path=repo_path, repo_dict=repo_dict, result=result
        )
        try:
            RepoDefaultsFile.from_dict(repo_dict, file_label=str(repo_path))
        except ConfigError as exc:
            result.add_error(str(exc))
    if user_resolved is not None and user_dict:
        try:
            UserConfigFile.from_dict(user_dict, file_label=str(user_resolved))
        except ConfigError as exc:
            result.add_error(str(exc))

    # If the schema rejected outright, the merged loader will too —
    # bail before we add a noisy stack trace.
    if result.errors:
        return result

    # Now load the merged view to drive the heuristic checks.
    try:
        merged = load_merged_repo_config(
            checkout=Path(checkout) if checkout is not None else None,
            user_path=user_resolved,
        )
    except ConfigError as exc:
        result.add_error(str(exc))
        return result

    # Operator-scoped checks (run on the user dict).
    _check_local_repos_paths(
        user_dict=merged.user_file, user_path=user_resolved, result=result
    )
    _check_writable_repos_have_settings(
        user_dict=merged.user_file, user_path=user_resolved, result=result
    )

    # Repo-side denylist re-check (the loader already enforces it; this
    # surfaces the same diagnostic via the validator output if a
    # caller bypassed the loader).
    if repo_path is not None:
        repo_persist = repo_dict.get("persist") or []
        if isinstance(repo_persist, list):
            _check_repo_persist_denylist(
                repo_path=repo_path,
                persist=[str(p) for p in repo_persist if isinstance(p, str)],
                result=result,
            )

    # Per-repo checks.
    repo_blocks = merged.repo_blocks or {}
    repo_root = Path(checkout) if checkout is not None else None
    for repo_name, block in repo_blocks.items():
        repo_label = (
            f"{user_resolved or '<user>'} :: repo_settings[{repo_name!r}]"
        )
        build_cmds = block.get("build_commands") or {}
        commands: list[str] = []
        if isinstance(build_cmds, dict):
            cmds_raw = build_cmds.get("commands") or []
            if isinstance(cmds_raw, list):
                commands = [str(c) for c in cmds_raw]

        watch_files_raw = block.get("watch_files") or []
        watch_files = (
            [str(f) for f in watch_files_raw]
            if isinstance(watch_files_raw, list)
            else []
        )

        persist_raw = block.get("persist") or []
        persist = (
            [str(p) for p in persist_raw]
            if isinstance(persist_raw, list)
            else []
        )

        checks_raw = block.get("checks") or []
        checks = checks_raw if isinstance(checks_raw, list) else []

        if commands:
            _check_install_paths_persisted(
                repo_label=repo_label,
                commands=commands,
                persist_entries=persist,
                result=result,
            )
            _check_build_context_needs_source(
                repo_label=repo_label,
                commands=commands,
                watch_files=watch_files,
                result=result,
            )
            _check_curl_in_private_mode(
                repo_label=repo_label,
                commands=commands,
                result=result,
            )
            _check_watch_files_match_commands(
                repo_label=repo_label,
                watch_files=watch_files,
                commands=commands,
                result=result,
            )

        # Makefile target check runs against the checkout root.
        if checks and repo_root is not None:
            _check_makefile_targets(
                repo_label=repo_label,
                repo_path=repo_root,
                checks=[c for c in checks if isinstance(c, dict)],
                result=result,
            )

        _check_persist_empty_dir(
            repo_label=repo_label,
            persist=persist,
            repo_path=repo_root,
            result=result,
        )
        _check_auth_mode_user_token(
            repo_label=repo_label,
            auth_mode=block.get("auth_mode"),
            result=result,
        )
        _check_checkpoint_repo_format(
            repo_label=repo_label,
            checkpoint_repo=block.get("checkpoint_repo"),
            result=result,
        )

    return result


__all__ = [
    "ValidationResult",
    "validate_repo_config",
]
