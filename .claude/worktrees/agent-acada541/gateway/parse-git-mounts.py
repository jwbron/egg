#!/usr/bin/env python3
"""Parse repositories.yaml and output git mount specifications.

Outputs one mount spec per line in format: source:destination:options
This allows the calling bash script to safely handle paths with spaces.
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Warning: PyYAML not installed, skipping dynamic git mounts", file=sys.stderr)
    sys.exit(0)


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <config_file> <home_dir>", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[1]
    home = sys.argv[2]

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        local_repos = config.get("local_repos", {})
        paths = local_repos.get("paths", [])

        for repo_path in paths:
            repo_path = Path(repo_path).expanduser()
            if not repo_path.exists():
                continue

            repo_name = repo_path.name
            git_dir = repo_path / ".git"

            if git_dir.is_file():
                # Worktree - read the actual git dir location
                with open(git_dir) as f:
                    content = f.read().strip()
                    if content.startswith("gitdir:"):
                        actual_git = content[7:].strip()
                        git_dir = Path(actual_git)

            if git_dir.exists():
                # Mount git directory to a known location
                # Needs RW for git fetch (FETCH_HEAD, refs) and object sync after push
                # No :z flag - SELinux relabeling disabled via --security-opt label=disable
                container_git_path = f"{home}/.git-main/{repo_name}"
                print(f"{git_dir}:{container_git_path}")

    except Exception as e:
        print(f"Warning: Failed to parse git mounts: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
