#!/usr/bin/env python3
"""Populate ``./repo-deps/`` for the sandbox image build.

``sandbox/Dockerfile`` Stage 1 does ``COPY repo-deps/ /tmp/repo-deps/``,
then runs ``docker-setup.py`` which reads ``manifest.json`` to drive
per-repo ``build_commands`` (e.g. ``uv sync``, ``pnpm install``) and
persist their output (``.venv``, ``node_modules``) into the image.

The manifest + watch files come from the host's ``repositories.yaml``;
this script materializes them into the build context. Without it,
``make build`` would seed an empty marker and the image would ship with
no prebuilt deps — see #2499.

Usage:
    scripts/prepare-sandbox-build-context.py             # writes ./repo-deps/
    scripts/prepare-sandbox-build-context.py <dir>       # writes <dir>/
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sandbox"))

from egg_lib.docker import populate_build_context  # noqa: E402


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "repo-deps"
    populate_build_context(target.resolve(), quiet=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
