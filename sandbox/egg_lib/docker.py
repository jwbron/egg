"""Build-context population for the egg sandbox image.

Image builds are driven by ``make build`` (which calls
``scripts/prepare-sandbox-build-context.py`` to populate ``./repo-deps/``
from ``repositories.yaml`` before ``docker build``). This module owns the
host-side build-context helpers: discovering local repo paths, copying
per-repo watch files, and writing the ``manifest.json`` that
``docker-setup.py`` reads during the image build.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .output import info, warn


def is_dangerous_dir(path: Path) -> bool:
    """Check if a directory is dangerous to mount (contains credentials)"""
    for dangerous in Config.DANGEROUS_DIRS:
        try:
            # Check if path is dangerous or contains dangerous
            if path.resolve() == dangerous.resolve():
                return True
            if path.resolve() in dangerous.resolve().parents:
                return True
            if dangerous.resolve() in path.resolve().parents:
                return True
        except Exception:
            pass
    return False


def _load_repos_config() -> dict[str, Any]:
    """Load repositories.yaml for build_commands configuration.

    Returns:
        Parsed config dict, or empty dict if not found.
    """
    config_path = Config.REPOS_CONFIG_FILE
    if not config_path.exists():
        return {}
    try:
        with config_path.open() as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _get_local_repo_path(config: dict[str, Any], repo_name: str) -> Path | None:
    """Find the local path for a repo from local_repos.paths config.

    Matches by checking if the repo name appears as the last component(s) of
    the local path (e.g., /home/user/projects/org/repo matches org/repo).

    Args:
        config: Parsed repositories.yaml config
        repo_name: Repository in "owner/repo" format

    Returns:
        Path to the local repo directory, or None if not found.
    """
    local_repos = config.get("local_repos", {})
    if not isinstance(local_repos, dict):
        return None
    paths = local_repos.get("paths", [])
    if not isinstance(paths, list):
        return None

    # Normalize repo name for matching
    repo_parts = repo_name.lower().split("/")

    for path_str in paths:
        path = Path(str(path_str)).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            continue
        # Check if the path ends with the repo name parts
        # e.g., /home/user/repos/org/repo -> parts [-2:] = ["org", "repo"]
        path_parts = [p.lower() for p in path.parts]
        if len(path_parts) >= len(repo_parts):
            if path_parts[-len(repo_parts) :] == repo_parts:
                return path
        # Also try matching just the repo name (without owner)
        if len(repo_parts) > 1 and path.name.lower() == repo_parts[-1]:
            return path

    return None


def populate_build_context(target_dir: Path, quiet: bool = False) -> None:
    """Populate ``target_dir`` with watch files + manifest for the sandbox build.

    For each repo with ``build_commands`` configured in ``repositories.yaml``:
    copies the declared ``watch_files`` from the local repo directory into
    ``<target_dir>/<owner--repo>/`` and writes a ``manifest.json`` describing
    the build commands and persist directories. ``docker-setup.py`` reads
    that manifest during the sandbox image build (Stage 1 of
    ``sandbox/Dockerfile``) to run per-repo build steps and persist their
    output (e.g. ``.venv``, ``node_modules``) into the image.

    Docker layer caching keys on the contents of ``target_dir``: the
    ``COPY repo-deps/`` layer only invalidates when watch files change,
    so unchanged dependency layers are reused across builds.

    When no repos declare ``build_commands`` and no ``extra_packages`` are
    configured, an ``.empty`` marker is written so the Dockerfile ``COPY``
    step still has a valid source.
    """
    config = _load_repos_config()
    repo_settings = config.get("repo_settings", {})
    if not isinstance(repo_settings, dict):
        repo_settings = {}

    repo_deps_dir = target_dir

    # Footgun guard: this function rmtrees its target. The Makefile passes
    # ``./repo-deps``, but the script is a public entry point and a stray
    # argument like ``/home/user/important-data`` would otherwise be wiped.
    # Refuse anything whose final segment isn't ``repo-deps``.
    if repo_deps_dir.name != "repo-deps":
        raise ValueError(
            f"populate_build_context refuses to operate on {repo_deps_dir!s}: "
            f"target directory must be named 'repo-deps' (got {repo_deps_dir.name!r})"
        )

    # Clean up old contents to avoid stale files
    if repo_deps_dir.exists():
        shutil.rmtree(repo_deps_dir, ignore_errors=True)

    has_any = False
    repos_with_local_path: set[str] = set()

    for repo_name, settings in repo_settings.items():
        if not isinstance(settings, dict):
            continue
        build_cmds = settings.get("build_commands")
        if not isinstance(build_cmds, dict):
            continue
        watch_files = build_cmds.get("watch_files", [])
        commands = build_cmds.get("commands", [])
        if not isinstance(watch_files, list) or not isinstance(commands, list):
            # Malformed yaml: tell the operator. Without this warn, a
            # repo with a typo in build_commands.watch_files / .commands
            # is silently dropped from both the watch-file copy step and
            # (via repos_with_local_path) the manifest, producing an
            # image with no per-repo build steps and no log line.
            #
            # Intentionally NOT gated on ``quiet``: this is operator
            # misconfiguration, not a recoverable per-file condition like
            # "watch file not found" or "local path not found". The other
            # warns in this function are quiet-gated because tests run with
            # quiet=True to suppress expected per-file noise; a malformed
            # build_commands block is a config bug we want surfaced
            # regardless.
            warn(f"build_commands: skipping {repo_name} — watch_files and commands must be lists")
            continue
        if not commands:
            continue

        # Find the local repo path
        local_path = _get_local_repo_path(config, repo_name)
        if local_path is None:
            if not quiet:
                warn(f"build_commands: local path not found for {repo_name}, skipping watch files")
            continue

        repos_with_local_path.add(repo_name)

        # Copy watch files
        repo_dir_name = repo_name.replace("/", "--")
        dest_dir = repo_deps_dir / repo_dir_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        copied_any = False
        for watch_file in watch_files:
            src_file = local_path / str(watch_file)

            # Defense-in-depth: validate path stays within repo boundary
            try:
                src_file.resolve().relative_to(local_path.resolve())
            except ValueError:
                warn(f"build_commands: watch file escapes repo boundary: {repo_name}/{watch_file}")
                continue

            if not src_file.exists() or not src_file.is_file():
                if not quiet:
                    warn(f"build_commands: watch file not found: {repo_name}/{watch_file}")
                continue

            # Defense-in-depth: don't follow symlinks that point outside the repo
            if src_file.is_symlink():
                resolved = src_file.resolve()
                if not resolved.is_relative_to(local_path.resolve()):
                    warn(
                        f"build_commands: watch file symlink escapes repo boundary: {repo_name}/{watch_file}"
                    )
                    continue

            # Preserve directory structure within the watch file path
            dest_file = dest_dir / str(watch_file)

            # Validate dest path stays within dest_dir
            try:
                dest_file.resolve().relative_to(dest_dir.resolve())
            except ValueError:
                warn(
                    f"build_commands: watch file dest escapes build context: {repo_name}/{watch_file}"
                )
                continue

            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
            copied_any = True

        if copied_any:
            has_any = True
            if not quiet:
                info(f"Copied watch files for {repo_name}")

    # Write a manifest.json so docker-setup.py can read it during the Docker build.
    # (repositories.yaml is not available in the build context)
    # Format: {"extra_packages": {"apt": [...], "dnf": [...]}, "build_commands": [...]}
    build_commands_list = []
    for repo_name, settings in repo_settings.items():
        if not isinstance(settings, dict):
            continue
        build_cmds = settings.get("build_commands")
        if not isinstance(build_cmds, dict):
            continue
        commands = build_cmds.get("commands", [])
        if not isinstance(commands, list) or not commands:
            continue
        # Skip repos whose local path wasn't found above. The host already
        # warned; emitting a manifest entry here would surface as a
        # downstream RuntimeError from docker-setup.py:run_build_commands
        # (watch files dir missing) which is just noise for the same root
        # cause. Keep the host warning as the single source of truth.
        if repo_name not in repos_with_local_path:
            continue
        watch_files = build_cmds.get("watch_files", [])
        if not isinstance(watch_files, list):
            watch_files = []
        persist_dirs = build_cmds.get("persist_dirs", [])
        if not isinstance(persist_dirs, list):
            persist_dirs = []
        persist_system_dirs = build_cmds.get("persist_system_dirs", [])
        if not isinstance(persist_system_dirs, list):
            persist_system_dirs = []
        build_commands_list.append(
            {
                "repo": repo_name,
                "watch_files": [str(f) for f in watch_files],
                "commands": [str(c) for c in commands],
                "persist_dirs": [str(d) for d in persist_dirs],
                "persist_system_dirs": [str(d) for d in persist_system_dirs],
            }
        )

    # Also include extra_packages so they're installed during the Docker build
    docker_setup_cfg = config.get("docker_setup", {})
    extra_pkgs = (
        docker_setup_cfg.get("extra_packages", {}) if isinstance(docker_setup_cfg, dict) else {}
    )
    if not isinstance(extra_pkgs, dict):
        extra_pkgs = {}
    apt_pkgs = extra_pkgs.get("apt", [])
    dnf_pkgs = extra_pkgs.get("dnf", [])
    generic_pkgs = extra_pkgs.get("packages", [])
    if not isinstance(apt_pkgs, list):
        apt_pkgs = []
    if not isinstance(dnf_pkgs, list):
        dnf_pkgs = []
    if not isinstance(generic_pkgs, list):
        generic_pkgs = []
    apt_pkgs = [str(p) for p in apt_pkgs + generic_pkgs]
    dnf_pkgs = [str(p) for p in dnf_pkgs + generic_pkgs]

    manifest_data: dict[str, Any] = {
        "extra_packages": {"apt": apt_pkgs, "dnf": dnf_pkgs},
        "build_commands": build_commands_list,
    }

    if build_commands_list or apt_pkgs or dnf_pkgs:
        repo_deps_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = repo_deps_dir / "manifest.json"
        with manifest_path.open("w") as f:
            json.dump(manifest_data, f, indent=2)
        if not quiet:
            info(
                f"Wrote build manifest ({len(build_commands_list)} repos, "
                f"{len(apt_pkgs)} apt pkgs, {len(dnf_pkgs)} dnf pkgs)"
            )
        has_any = True

    if not has_any:
        # Always create repo-deps with an empty marker so Dockerfile COPY doesn't fail
        repo_deps_dir.mkdir(parents=True, exist_ok=True)
        (repo_deps_dir / ".empty").touch()
