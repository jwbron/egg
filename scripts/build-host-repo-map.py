#!/usr/bin/env python3
"""Build EGG_HOST_REPO_MAP from ~/.config/egg/repositories.yaml.

The orchestrator reads ``EGG_HOST_REPO_MAP`` — a JSON ``owner/repo →
host_path`` mapping — to know where each repo lives on the host when it
builds ``hostPath`` mounts for spawned agent pods.

This script derives the map from ``local_repos.paths`` in
``repositories.yaml``: for each path, it reads the repo's ``origin``
remote URL to recover the ``owner/repo`` identifier, pairs that with the
host path, and prints the resulting JSON to stdout.

Usage:
    scripts/build-host-repo-map.py             # uses ~/.config/egg/repositories.yaml
    scripts/build-host-repo-map.py <path>      # explicit config path

Emits ``{}`` (and exits 0) when the config is missing or lists no repos
— callers that want to fail on empty input must check the output.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(1)


# Supported remote URL formats:
#   git@host:owner/repo[.git]               (scp-like SSH)
#   ssh://[user@]host[:port]/owner/repo[.git]
#   https://host[:port]/owner/repo[.git][/]
_SCP_SSH_REMOTE = re.compile(r"^[^@]+@[^:]+:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$")
_URL_REMOTE = re.compile(r"^(?:ssh|https?)://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


def parse_owner_repo(remote_url: str) -> str | None:
    """Extract ``owner/repo`` from a git remote URL, or None if unparseable."""
    for pattern in (_SCP_SSH_REMOTE, _URL_REMOTE):
        match = pattern.match(remote_url.strip())
        if match:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def get_origin_url(repo_path: Path) -> str | None:
    """Return the ``origin`` remote URL for a git repo, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def build_map(config_path: Path) -> dict[str, str]:
    """Read ``local_repos.paths`` and build the owner/repo → host_path map."""
    if not config_path.exists():
        return {}

    try:
        with config_path.open() as fh:
            config = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        print(f"WARN: failed to parse {config_path}: {exc}", file=sys.stderr)
        return {}

    local_repos = config.get("local_repos") or {}
    paths = local_repos.get("paths") or [] if isinstance(local_repos, dict) else []

    mapping: dict[str, str] = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_dir():
            continue
        remote_url = get_origin_url(path)
        if not remote_url:
            print(
                f"WARN: no origin remote for {path}, skipping",
                file=sys.stderr,
            )
            continue
        owner_repo = parse_owner_repo(remote_url)
        if not owner_repo:
            print(
                f"WARN: could not parse owner/repo from {remote_url!r}, skipping {path}",
                file=sys.stderr,
            )
            continue
        # Last-wins if two paths resolve to the same owner/repo (e.g. two
        # checkouts of the same repo).  The later entry in paths silently
        # shadows the earlier one.
        mapping[owner_repo] = str(path)

    return mapping


def main() -> None:
    if len(sys.argv) > 2:
        print(f"Usage: {sys.argv[0]} [config_path]", file=sys.stderr)
        sys.exit(2)

    if len(sys.argv) == 2:
        config_path = Path(sys.argv[1]).expanduser()
    else:
        config_path = Path.home() / ".config" / "egg" / "repositories.yaml"

    mapping = build_map(config_path)
    print(json.dumps(mapping, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
